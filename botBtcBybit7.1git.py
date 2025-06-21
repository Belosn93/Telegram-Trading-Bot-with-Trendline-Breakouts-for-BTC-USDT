import ccxt.async_support as ccxt
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import asyncio
from telegram import Bot
from telegram.error import TelegramError
from scipy.stats import linregress

# --- Настройки ---
TELEGRAM_TOKEN = ""
TELEGRAM_CHAT_ID =
SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "15m"
MAX_RISK_USD = 1.0
MIN_RR_RATIO = 3.0
SAFETY_BUFFER = 0.005
MIN_TOUCHES = 2
MAX_TOUCH_GAP_MIN = 120  # 2 часов между касаниями  720
VOLUME_WINDOW = 20

telegram_bot = Bot(token=TELEGRAM_TOKEN)

exchange = ccxt.bybit({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
})


# --- Уведомления ---
async def send_startup_message():
    try:
        await telegram_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="✅ Бот запущен и сканирует BTC/USDT на 15-минутках."
        )
    except TelegramError as e:
        print(f"❌ Ошибка Telegram (запуск): {e}")


async def send_shutdown_message():
    try:
        await telegram_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="🛑 Бот остановлен. Мониторинг прекращён."
        )
    except TelegramError as e:
        print(f"❌ Ошибка Telegram (остановка): {e}")


# --- Загрузка данных ---
async def fetch_ohlcv():
    candles = await exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df


async def get_current_price():
    ticker = await exchange.fetch_ticker(SYMBOL)
    return ticker['last']


# --- Трендовые линии ---
def find_trendline_breakout(df):
    highs = df['high'].values
    touches = []
    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            touches.append((i, highs[i]))

    if len(touches) < MIN_TOUCHES:
        return None

    x = [t[0] for t in touches]
    y = [t[1] for t in touches]
    slope, intercept, *_ = linregress(x, y)

    trendline = [slope * i + intercept for i in range(len(df))]
    df['trendline'] = trendline

    last_close = df['close'].iloc[-2]
    current_close = df['close'].iloc[-1]
    last_trend = df['trendline'].iloc[-2]
    current_trend = df['trendline'].iloc[-1]

    last_volume = df['volume'].iloc[-1]
    avg_volume = df['volume'].iloc[-VOLUME_WINDOW:].mean()

    # Проверка условий пробоя
    if last_close < last_trend and current_close > current_trend and last_volume > avg_volume:
        times = [df['timestamp'].iloc[t[0]] for t in touches]
        time_gaps = [(times[i] - times[i - 1]).total_seconds() / 60 for i in range(1, len(times))]
        if all(g < MAX_TOUCH_GAP_MIN for g in time_gaps):
            return len(df) - 1

    return None


# --- Расчёт SL, TP и пр. ---
def find_nearest_resistance(df, entry_idx):
    highs_ahead = df['high'].iloc[entry_idx + 1:]
    return highs_ahead.max() if not highs_ahead.empty else df['close'].iloc[entry_idx] * (1 + 0.03)


def calculate_sl_tp_liq(df, entry_idx):
    entry_price = df['close'].iloc[entry_idx]
    sl_price = min(df['low'].iloc[entry_idx - 5:entry_idx]) * (1 - SAFETY_BUFFER)
    tp_price = find_nearest_resistance(df, entry_idx)
    rr_ratio = (tp_price - entry_price) / (entry_price - sl_price)
    if rr_ratio < MIN_RR_RATIO:
        return None

    leverage = min(int(1 / (1 - sl_price / entry_price)), 100)
    risk_per_contract = entry_price - sl_price
    position_size = min(MAX_RISK_USD / risk_per_contract, 1000) if risk_per_contract > 0 else 0
    liq_price = entry_price * (1 - (1 / leverage)) * (1 - SAFETY_BUFFER)
    return sl_price, tp_price, liq_price, leverage, position_size, entry_price, rr_ratio


# --- Отправка сигнала ---
async def send_signal(df, entry_idx, sl, tp, liq, leverage, position_size, current_price, rr_ratio):
    plt.style.use('dark_background')
    plt.figure(figsize=(12, 6))
    plt.plot(df['close'], label='Цена', color='cyan')
    plt.plot(df['trendline'], label='Трендовая', linestyle='--', color='magenta')
    plt.axhline(y=sl, color='red', linestyle='--', label='SL')
    plt.axhline(y=tp, color='blue', linestyle='--', label='TP')
    plt.axhline(y=liq, color='orange', linestyle=':', label='Ликв.')
    plt.title(f"BTC 15M | RR 1:{rr_ratio:.1f} | Плечо: {leverage}x")
    plt.legend()
    plt.savefig('signal.png', bbox_inches='tight')
    plt.close()

    message = (
        f"🚀 **Лонг сигнал (BTC/USDT Futures)**\n"
        f"⏰ Время: `{datetime.now().strftime('%H:%M:%S')}`\n"
        f"📈 Цена входа: `{df['close'].iloc[entry_idx]:.2f}`\n"
        f"🛑 SL: `{sl:.2f}`\n"
        f"🎯 TP: `{tp:.2f}`\n"
        f"💀 Ликвидация: `{liq:.2f}`\n"
        f"📊 Плечо: `{leverage}x`\n"
        f"📉 RR: `1:{rr_ratio:.1f}`\n"
        f"💵 Позиция: `{position_size:.2f} USDT`\n"
    )

    try:
        with open('signal.png', 'rb') as photo:
            await telegram_bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=photo,
                caption=message,
                parse_mode='Markdown'
            )
    except TelegramError as e:
        print(f"❌ Ошибка Telegram: {e}")


# --- Основной цикл ---
async def main_loop():
    print("🔁 Сканер BTC/USDT (15m) запущен.")
    await send_startup_message()
    try:
        while True:
            df = await fetch_ohlcv()
            entry_idx = find_trendline_breakout(df)
            if entry_idx:
                result = calculate_sl_tp_liq(df, entry_idx)
                if result:
                    sl, tp, liq, leverage, position_size, entry_price, rr_ratio = result
                    current_price = await get_current_price()
                    if liq < sl and position_size > 0:
                        await send_signal(df, entry_idx, sl, tp, liq, leverage, position_size, current_price, rr_ratio)
                        await asyncio.sleep(60 * 15)
                        continue
            await asyncio.sleep(60)
    except Exception as e:
        print(f"⚠ Ошибка: {e}")
    finally:
        await send_shutdown_message()
        await exchange.close()


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("🛑 Остановка по Ctrl+C")


