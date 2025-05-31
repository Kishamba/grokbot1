import os
import sys
import time
import logging
import pandas as pd
from pybit.unified_trading import HTTP, WebSocket
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange
from dotenv import load_dotenv
import json
import requests
import threading
import importlib.util

# Установка зависимостей: pip install pybit ta pandas python-telegram-bot==13.15 requests python-dotenv

# Проверка версии Python
if sys.version_info.major == 3 and sys.version_info.minor >= 13:
    logging.warning("Python 3.13 не поддерживается. Используйте Python 3.12 или ниже и создайте новое виртуальное окружение: /opt/homebrew/bin/python3.12 -m venv venv")
    telegram_enabled = False
else:
    try:
        spec = importlib.util.find_spec("telegram.ext")
        if spec is None:
            logging.warning("Библиотека python-telegram-bot не найдена. Установи её с помощью: pip install python-telegram-bot==13.15")
            telegram_enabled = False
        else:
            from telegram.ext import Updater, CommandHandler, MessageHandler, Filters  # Для обработки Telegram-сообщений
            telegram_enabled = True
    except Exception as e:
        logging.error(f"Ошибка при проверке библиотеки telegram: {e}")
        telegram_enabled = False

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Файл для логирования сделок
TRADES_LOG_FILE = "trades_log.json"

# Telegram настройки
TELEGRAM_TOKEN = "8012235659:AAEzFYu-WSRR4R28YNhIRm-6AXHVnCyyqxM"
TELEGRAM_CHAT_ID = "228860864"  # Твой Chat ID

# Загрузка API-ключей из файла .env
load_dotenv()
api_key = os.getenv('BYBIT_API_KEY')
api_secret = os.getenv('BYBIT_API_SECRET')

# Проверка наличия API-ключей
if not api_key or not api_secret:
    logging.error("API-ключи не найдены в файле .env")
    exit(1)

# Подключение к Bybit testnet (REST API)
try:
    session = HTTP(
        testnet=True,
        api_key=api_key,
        api_secret=api_secret
    )
    logging.info("Успешное подключение к Bybit testnet (REST API)")
except Exception as e:
    logging.error(f"Ошибка подключения к Bybit REST API: {e}")
    exit(1)

# Параметры торговли
SYMBOL = "BTCUSDT"
LEVERAGE = 5
POSITION_SIZE = 0.001  # Размер позиции в BTC
STOP_LOSS_PCT = 0.005  # 0.5%
TAKE_PROFIT_PCT = 0.01  # 1%
MAX_LOSS_PCT = 0.05  # Максимальный убыток 5%
UPDATE_INTERVAL = 60  # Обновление каждые 60 секунд

# Глобальные переменные для управления рисками
initial_balance = 0
total_pnl = 0

# Глобальные переменные для хранения индикаторов
latest_indicators = {
    "rsi": None,
    "ema_fast": None,
    "ema_slow": None,
    "atr": None,
    "macd": None,
    "signal_line": None,
    "signal": None
}

# Установка кредитного плеча
try:
    session.set_leverage(
        category="linear",
        symbol=SYMBOL,
        buyLeverage=str(LEVERAGE),
        sellLeverage=str(LEVERAGE)
    )
    logging.info(f"Установлено кредитное плечо {LEVERAGE}x для {SYMBOL}")
except Exception as e:
    if "110043" in str(e):
        logging.info(f"Плечо уже установлено или не требует изменений: {e}")
    else:
        logging.error(f"Ошибка при настройке плеча: {e}")
        exit(1)

# Функция для отправки уведомлений в Telegram
def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        }
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            logging.error(f"Ошибка отправки в Telegram: {response.text}")
    except Exception as e:
        logging.error(f"Ошибка отправки уведомления в Telegram: {e}")

# Настройка Telegram-бота для обработки команд (только если включен)
def setup_telegram_bot():
    if telegram_enabled:
        try:
            updater = Updater(TELEGRAM_TOKEN, use_context=True)
            dispatcher = updater.dispatcher

            # Обработчик команды /start
            def start(update, context):
                update.message.reply_text("Привет, я Kishambot! Я торгую на Bybit testnet. Используй /status для проверки статуса.")

            # Обработчик команды /status
            def status(update, context):
                if SELECTED_SYMBOL is None:
                    logging.info("Статус отправлен для SELECTED_SYMBOL: None")
                    send_telegram_message("📊 Статус бота:\n  Монета не выбрана. Выберите монету через меню.")
                    return
                if all(v is not None for v in latest_indicators.values()):
                    response = (
                        f"Статус бота:\n"
                        f"RSI: {latest_indicators['rsi']:.2f}\n"
                        f"EMA12: {latest_indicators['ema_fast']:.2f}\n"
                        f"EMA26: {latest_indicators['ema_slow']:.2f}\n"
                        f"ATR: {latest_indicators['atr']:.2f}\n"
                        f"Сигнал: {latest_indicators['signal'] if latest_indicators['signal'] else 'Нет сигнала'}"
                    )
                else:
                    response = "Данные индикаторов еще не доступны. Подожди немного!"
                update.message.reply_text(response)

            # Обработчик текстовых сообщений (кроме команд)
            def unknown(update, context):
                update.message.reply_text("Я понимаю только команды /start и /status. Используй их!")

            # Регистрация обработчиков
            dispatcher.add_handler(CommandHandler("start", start))
            dispatcher.add_handler(CommandHandler("status", status))
            dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, unknown))

            # Запуск бота
            updater.start_polling()
            logging.info("Telegram-бот запущен для обработки команд")
            updater.idle()
        except Exception as e:
            logging.error(f"Ошибка настройки Telegram-бота: {e}")

# Проверка баланса
def check_balance():
    try:
        balance_unified = session.get_wallet_balance(accountType="UNIFIED")
        logging.info(f"Ответ API для UNIFIED: {balance_unified}")
        usdt_balance = _extract_usdt_balance(balance_unified)

        if usdt_balance == 0:
            balance_contract = session.get_wallet_balance(accountType="CONTRACT")
            logging.info(f"Ответ API для CONTRACT: {balance_contract}")
            usdt_balance = _extract_usdt_balance(balance_contract)

        if usdt_balance == 0:
            logging.warning("USDT не найден на балансе. Проверьте перевод средств в Derivatives.")
        else:
            logging.info(f"Итоговый баланс USDT: {usdt_balance}")
        return usdt_balance
    except Exception as e:
        logging.error(f"Ошибка при проверке баланса: {e}")
        return 0

def _extract_usdt_balance(balance_data):
    try:
        coins = balance_data.get('result', {}).get('list', [{}])[0].get('coin', [])
        for coin in coins:
            if coin.get('coin') == 'USDT':
                for key in ['availableBalance', 'availableToWithdraw', 'walletBalance', 'equity', 'free']:
                    if key in coin and coin[key]:
                        balance = float(coin[key])
                        logging.info(f"Найден баланс USDT по ключу {key}: {balance}")
                        return balance
                break
        return 0
    except Exception as e:
        logging.error(f"Ошибка при извлечении баланса: {e}")
        return 0

# Функция для проверки ликвидности
def check_liquidity(current_price, qty):
    required_margin = (current_price * qty) / LEVERAGE
    current_balance = check_balance()
    if current_balance < required_margin * 1.1:
        logging.error(f"Недостаточно средств для открытия позиции. Требуется: {required_margin}, доступно: {current_balance}")
        return False
    return True

# Функция для получения текущей цены через WebSocket
def start_websocket(symbol=SYMBOL):
    try:
        ws = WebSocket(
            testnet=True,
            channel_type="linear"
        )
        
        def handle_ticker(data):
            try:
                price = float(data['data']['lastPrice'])
                logging.info(f"WebSocket: Текущая цена {symbol}: {price} USDT")
                print(f"WebSocket: Текущая цена {symbol}: {price} USDT")
            except Exception as e:
                logging.error(f"Ошибка обработки WebSocket данных: {e}")

        ws.ticker_stream(symbol=symbol, callback=handle_ticker)
        logging.info("WebSocket запущен для получения цен")
        return ws
    except Exception as e:
        logging.error(f"Ошибка подключения к WebSocket: {e}")
        return None

# Функция для получения 5-минутных свечей
def get_klines(symbol=SYMBOL, interval=5, limit=100):
    try:
        klines = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=interval,
            limit=limit
        )
        df = pd.DataFrame(
            klines['result']['list'],
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover']
        )
        df['timestamp'] = pd.to_datetime(df['timestamp'].astype(float), unit='ms')
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        logging.info(f"Получено {len(df)} свечей для {symbol}")
        return df
    except Exception as e:
        logging.error(f"Ошибка при получении свечей для {symbol}: {e}")
        return None

# Функция для расчета индикаторов RSI, EMA и ATR
def calculate_indicators(df):
    try:
        if len(df) < 26:
            logging.error("Недостаточно данных для расчета индикаторов")
            return None, None, None, None, None, None
        rsi = RSIIndicator(close=df['close'], window=14).rsi()
        ema_fast = EMAIndicator(close=df['close'], window=12).ema_indicator()
        ema_slow = EMAIndicator(close=df['close'], window=26).ema_indicator()
        atr = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()
        rsi_value = rsi.iloc[-1]
        ema_fast_value = ema_fast.iloc[-1]
        ema_slow_value = ema_slow.iloc[-1]
        atr_value = atr.iloc[-1]
        macd = MACD(close=df['close'], window_slow=26, window_fast=12, window_sign=9)
        macd_line = macd.macd()[-1]
        signal_line = macd.macd_signal()[-1]
        logging.info(f"RSI: {rsi_value:.2f}, EMA12: {ema_fast_value:.2f}, EMA26: {ema_slow_value:.2f}, ATR: {atr_value:.2f}")
        return rsi_value, ema_fast_value, ema_slow_value, atr_value, macd_line, signal_line
    except Exception as e:
        logging.error(f"Ошибка при расчете индикаторов: {e}")
        return None, None, None, None, None, None

# Функция для генерации торговых сигналов
def generate_signal(rsi, ema_fast, ema_slow, atr):
    try:
        if any(v is None for v in [rsi, ema_fast, ema_slow, atr]):
            return None
        avg_atr = atr.mean()  # Среднее значение ATR за период
        # Условия для покупки
        if rsi < 60 and ema_fast > ema_slow and atr > avg_atr * 0.5:
            return "Buy"
        # Условия для продажи
        elif rsi > 60 and ema_fast < ema_slow and atr > avg_atr * 0.5:
            return "Sell"
        return None
    except Exception as e:
        logging.error(f"Ошибка при генерации сигнала: {e}")
        return None

# Функция для проверки открытых позиций
def check_position(symbol=SYMBOL):
    try:
        position = session.get_positions(category="linear", symbol=symbol)
        position_info = position['result']['list'][0]
        qty = float(position_info['size'])
        side = position_info['side']
        entry_price = float(position_info['avgPrice']) if qty > 0 else 0
        return qty, side, entry_price
    except Exception as e:
        logging.error(f"Ошибка при проверке позиций: {e}")
        return 0, None, 0

# Функция для размещения ордера
def place_order(side, price, qty):
    try:
        order = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Market",
            qty=qty,
            stopLoss=str(price * (1 - STOP_LOSS_PCT if side == "Buy" else 1 + STOP_LOSS_PCT)),
            takeProfit=str(price * (1 + TAKE_PROFIT_PCT if side == "Buy" else 1 - TAKE_PROFIT_PCT))
        )
        logging.info(f"Ордер размещен: {side}, цена: {price}, объем: {qty}")
        send_telegram_message(f"📈 Открыта позиция: {side} {SYMBOL} @ {price}, объем: {qty}")
        log_trade("open", side, price, qty, status="executed")
        return order
    except Exception as e:
        logging.error(f"Ошибка при размещении ордера: {e}")
        return None

# Функция для закрытия позиции
def close_position(side, qty, current_price, initial_price):
    try:
        close_side = "Sell" if side == "Buy" else "Buy"
        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=close_side,
            orderType="Market",
            qty=qty
        )
        logging.info(f"Позиция закрыта: {close_side}, цена: {current_price}, объем: {qty}")
        send_telegram_message(f"📉 Закрыта позиция: {close_side} {SYMBOL} @ {current_price}, объем: {qty}")
        log_trade("close", close_side, current_price, qty, status="closed")
    except Exception as e:
        logging.error(f"Ошибка при закрытии позиции: {e}")

# Функция для логирования сделок
def log_trade(action, side, price, qty, status="executed"):
    trade = {
        "timestamp": str(pd.Timestamp.now()),
        "action": action,
        "side": side,
        "price": price,
        "qty": qty,
        "status": status,
        "symbol": SYMBOL,
        "pnl": 0.0 if action == "open" else (price - initial_price) * qty if side == "Buy" else (initial_price - price) * qty
    }
    try:
        with open(TRADES_LOG_FILE, 'a') as f:
            f.write(json.dumps(trade) + "\n")
        logging.info(f"Сделка записана: {trade}")
    except Exception as e:
        logging.error(f"Ошибка при записи в лог сделок: {e}")

# Основная торговая функция
def trading_loop():
    global initial_balance, total_pnl, latest_indicators
    # Устанавливаем начальный баланс
    initial_balance = check_balance()
    if initial_balance < POSITION_SIZE * 10000:
        logging.error("Недостаточно средств на балансе. Пополните USDT на testnet или переведите в Derivatives.")
        return

    # Запуск WebSocket для цен в реальном времени
    ws = start_websocket()
    if not ws:
        print("Не удалось запустить WebSocket")
        return

    last_update = 0
    try:
        while True:
            current_time = time.time()
            # Обновляем данные каждые UPDATE_INTERVAL секунд
            if current_time - last_update >= UPDATE_INTERVAL:
                # Получение свечей
                df = get_klines()
                if df is not None:
                    print("\n5-минутные свечи (последние 5):")
                    print(df.tail(5))
                    # Расчет индикаторов
                    rsi, ema_fast, ema_slow, atr, macd_line, signal_line = calculate_indicators(df)
                    if all(v is not None for v in [rsi, ema_fast, ema_slow, atr, macd_line, signal_line]):
                        print(f"\nИндикаторы для последней свечи:")
                        print(f"RSI: {rsi:.2f}")
                        print(f"EMA12: {ema_fast:.2f}")
                        print(f"EMA26: {ema_slow:.2f}")
                        print(f"ATR: {atr:.2f}")
                        # Сохранение индикаторов для команды /status
                        latest_indicators["rsi"] = rsi
                        latest_indicators["ema_fast"] = ema_fast
                        latest_indicators["ema_slow"] = ema_slow
                        latest_indicators["atr"] = atr
                        latest_indicators["macd"] = macd_line
                        latest_indicators["signal_line"] = signal_line
                        # Генерация сигнала
                        signal = generate_signal(rsi, ema_fast, ema_slow, atr)
                        latest_indicators["signal"] = signal
                        print(f"Сигнал: {signal if signal else 'Нет сигнала'}")

                        # Проверка текущей позиции
                        position_qty, position_side, entry_price = check_position()
                        current_price = float(session.get_tickers(category="linear", symbol=SYMBOL)['result']['list'][0]['lastPrice'])

                        # Проверка убытков
                        if position_qty > 0:
                            unrealized_pnl = (current_price - entry_price) * position_qty if position_side == "Buy" else (entry_price - current_price) * position_qty
                            total_pnl += unrealized_pnl
                            if total_pnl < 0 and abs(total_pnl) > initial_balance * MAX_LOSS_PCT:
                                logging.error(f"Достигнут максимальный убыток {MAX_LOSS_PCT*100}%: {abs(total_pnl)} USDT")
                                close_position(position_side, position_qty, current_price, entry_price)
                                send_telegram_message(f"⚠️ Бот остановлен: достигнут максимальный убыток {abs(total_pnl)} USDT")
                                break

                            # Проверка стоп-лосса и тейк-профита
                            if position_side == "Buy":
                                if current_price <= entry_price * (1 - STOP_LOSS_PCT) or current_price >= entry_price * (1 + TAKE_PROFIT_PCT):
                                    close_position(position_side, position_qty, current_price, entry_price)
                            else:  # Sell
                                if current_price >= entry_price * (1 + STOP_LOSS_PCT) or current_price <= entry_price * (1 - TAKE_PROFIT_PCT):
                                    close_position(position_side, position_qty, current_price, entry_price)

                        # Размещение ордера
                        if signal and position_qty == 0:
                            if check_liquidity(current_price, POSITION_SIZE):
                                place_order(signal, current_price, POSITION_SIZE)
                            else:
                                print("Ордер не размещен: недостаточно ликвидности")
                        else:
                            print("Ордер не размещен: нет сигнала или открыта позиция")
                    else:
                        print("Не удалось рассчитать индикаторы")
                else:
                    print("Не удалось получить свечи")
                last_update = current_time

            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Программа остановлена пользователем")
        ws.exit()

# Основная функция
def main():
    # Запуск Telegram-бота в отдельном потоке (если включен)
    if telegram_enabled:
        telegram_thread = threading.Thread(target=setup_telegram_bot)
        telegram_thread.daemon = True  # Поток завершится, когда завершится основной
        telegram_thread.start()

    # Запуск торговой логики в основном потоке
    trading_loop()

if __name__ == "__main__":
    main()