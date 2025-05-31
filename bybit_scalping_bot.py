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
import numpy as np
from datetime import datetime
import webbrowser
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

# Установка зависимостей: pip install pybit ta pandas requests python-dotenv

# Проверка версии Python
if sys.version_info.major == 3 and sys.version_info.minor >= 13:
    logging.warning("Python 3.13 не поддерживается. Используйте Python 3.12 или ниже и создайте новое виртуальное окружение: /opt/homebrew/bin/python3.12 -m venv venv")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Файл для логирования сделок
TRADES_LOG_FILE = "trades_log.json"

# Telegram настройки (для уведомлений)
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

# Глобальные переменные для управления рисками и позициями
initial_balance = 0
total_pnl = 0
positions = {}  # {symbol: {"entry_price": float, "qty": float, "side": str, "invested": float}}

# Глобальные переменные для хранения индикаторов
latest_indicators = {
    "rsi": None,
    "ema_fast": None,
    "ema_slow": None,
    "atr": None,
    "macd": None,
    "signal_line": None,
    "signal": None,
    "candle_pattern": None
}

# Глобальные переменные для Telegram
TELEGRAM_OFFSET = None

# Глобальная переменная для хранения выбранной монеты
SELECTED_SYMBOL = None

# Глобальная переменная для хранения WebSocket
ws = None

# Глобальная переменная для хранения топ-5 волатильных монет
TOP_VOLATILE_COINS = []

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

# Обновлённая функция для отправки сообщений в Telegram
def send_telegram_message(message, reply_markup=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        if reply_markup:
            logging.info(f"Отправка сообщения с reply_markup: {reply_markup}")
            payload["reply_markup"] = reply_markup
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            logging.info(f"Уведомление успешно отправлено в Telegram: {message}")
        else:
            logging.error(f"Ошибка отправки уведомления в Telegram: {response.text}")
    except Exception as e:
        logging.error(f"Ошибка при отправке уведомления в Telegram: {e}")

# Функция для проверки баланса
def check_balance():
    try:
        balance_unified = session.get_wallet_balance(accountType="UNIFIED")
        logging.info(f"Ответ API для UNIFIED: {balance_unified}")
        usdt_balance = _extract_usdt_balance(balance_unified)
        print(f"Баланс USDT: {usdt_balance}")  # Отладочный вывод
        if usdt_balance == 0:
            balance_contract = session.get_wallet_balance(accountType="CONTRACT")
            logging.info(f"Ответ API для CONTRACT: {balance_contract}")
            usdt_balance = _extract_usdt_balance(balance_contract)
            print(f"Баланс USDT (Contract): {usdt_balance}")  # Отладочный вывод
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
def check_liquidity(price, qty, symbol=None):
    if symbol is None:
        symbol = SELECTED_SYMBOL
    try:
        orderbook = session.get_orderbook(category="linear", symbol=symbol, limit=5)
        bids = orderbook['result']['b']
        asks = orderbook['result']['a']
        if not bids or not asks:
            return False
        total_bid_qty = sum(float(bid[1]) for bid in bids)
        total_ask_qty = sum(float(ask[1]) for ask in asks)
        required_qty = qty * price
        return total_bid_qty >= required_qty and total_ask_qty >= required_qty
    except Exception as e:
        logging.error(f"Ошибка при проверке ликвидности для {symbol}: {e}")
        return False

# Функция для получения текущей цены через WebSocket
def start_websocket(symbol=None):
    if symbol is None:
        logging.error("Ошибка: symbol не указан для WebSocket")
        return None
    try:
        ws_instance = WebSocket(
            testnet=True,
            channel_type="linear"
        )
        
        def handle_ticker(data):
            try:
                price = float(data['data']['lastPrice'])
                if not hasattr(handle_ticker, 'last_price') or handle_ticker.last_price != price:
                    logging.info(f"WebSocket: Текущая цена {data['data']['symbol']}: {price} USDT")
                    print(f"WebSocket: Текущая цена {data['data']['symbol']}: {price} USDT")
                    handle_ticker.last_price = price
            except Exception as e:
                logging.error(f"Ошибка обработки WebSocket данных: {e}")

        ws_instance.ticker_stream(symbol=symbol, callback=handle_ticker)
        logging.info(f"WebSocket запущен для получения цен {symbol}")
        return ws_instance
    except Exception as e:
        logging.error(f"Ошибка подключения к WebSocket: {e}")
        return None

# Функция для получения 5-минутных свечей
def get_klines(symbol=None):
    if symbol is None:
        symbol = SELECTED_SYMBOL
    try:
        response = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=5,
            limit=500
        )
        klines = response['result']['list']
        if not klines:
            logging.error(f"Не удалось получить свечи для {symbol}")
            return None
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        df['timestamp'] = pd.to_datetime(df['timestamp'].astype(float), unit='ms')
        for col in ['open', 'high', 'low', 'close', 'volume', 'turnover']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna()
        df = df.sort_values('timestamp').drop_duplicates(subset='timestamp', keep='last')
        logging.info(f"Получено {len(df)} свечей для {symbol}")
        if df.empty or df['close'].isna().any():
            logging.error(f"Данные свечей содержат пропуски или пусты: {df.tail()}")
            return None
        # Проверка на достаточное количество данных
        if len(df) < 26:
            logging.error(f"Недостаточно данных для расчета индикаторов: {len(df)} свечей")
            return None
        # Заполнение пропусков методом forward fill
        df = df.set_index('timestamp').resample('5min').ffill().reset_index()
        latest_time = df['timestamp'].iloc[-1]
        if (pd.Timestamp.now() - latest_time).total_seconds() > 3600:
            logging.warning(f"Данные устарели: последняя свеча {latest_time}, текущее время {pd.Timestamp.now()}")
        return df
    except Exception as e:
        logging.error(f"Ошибка при получении свечей для {symbol}: {e}")
        return None

# Функция для поиска свечных паттернов
def detect_candle_patterns(df):
    """Поиск свечных паттернов с расширенным набором паттернов."""
    if len(df) < 2:
        return None
    latest = df.iloc[-1]
    previous = df.iloc[-2]
    
    # Вычисление тела и диапазона свечей
    body_latest = abs(latest['close'] - latest['open'])
    body_previous = abs(previous['close'] - previous['open'])
    range_latest = latest['high'] - latest['low']
    range_previous = previous['high'] - previous['low']
    
    # Doji (маленькое тело относительно диапазона)
    if body_latest / range_latest < 0.1 and body_latest < 0.1 * range_previous:
        return "Doji"
    
    # Bullish Engulfing
    if (previous['close'] < previous['open'] and  # Медвежья свеча
        latest['close'] > latest['open'] and      # Бычья свеча
        latest['open'] < previous['close'] and    # Открытие ниже закрытия предыдущей
        latest['close'] > previous['open'] and    # Закрытие выше открытия предыдущей
        body_latest > body_previous):
        return "Bullish Engulfing"
    
    # Bearish Engulfing
    if (previous['close'] > previous['open'] and  # Бычья свеча
        latest['close'] < latest['open'] and      # Медвежья свеча
        latest['open'] > previous['close'] and    # Открытие выше закрытия предыдущей
        latest['close'] < previous['open'] and    # Закрытие ниже открытия предыдущей
        body_latest > body_previous):
        return "Bearish Engulfing"
    
    # Hammer (бычий паттерн)
    lower_shadow = latest['open'] - latest['low'] if latest['close'] > latest['open'] else latest['close'] - latest['low']
    upper_shadow = latest['high'] - latest['close'] if latest['close'] > latest['open'] else latest['high'] - latest['open']
    if (lower_shadow >= 2 * body_latest and body_latest / range_latest < 0.2 and latest['close'] > latest['open']):
        return "Hammer"
    
    # Shooting Star (медвежий паттерн)
    if (upper_shadow >= 2 * body_latest and body_latest / range_latest < 0.2 and latest['close'] < latest['open']):
        return "Shooting Star"
    
    # Morning Star (бычий паттерн, 3 свечи)
    if len(df) >= 3:
        third = df.iloc[-1]  # Последняя свеча
        second = df.iloc[-2]  # Средняя свеча
        first = df.iloc[-3]   # Первая свеча
        body_second = abs(second['close'] - second['open'])
        range_second = second['high'] - second['low']
        mid_first = (first['open'] + first['close']) / 2
        if (first['close'] < first['open'] and  # Первая свеча медвежья
            body_second / range_second < 0.1 and  # Вторая свеча с маленьким телом
            third['close'] > third['open'] and    # Третья свеча бычья
            third['close'] > mid_first):          # Закрытие третьей выше середины первой
            return "Morning Star"
    
    # Bullish Harami
    if (previous['close'] < previous['open'] and  # Первая свеча медвежья
        latest['close'] > latest['open'] and      # Вторая свеча бычья
        latest['open'] >= previous['close'] and   # Вторая свеча внутри тела первой
        latest['close'] <= previous['open']):
        return "Bullish Harami"
    
    # Bearish Harami
    if (previous['close'] > previous['open'] and  # Первая свеча бычья
        latest['close'] < latest['open'] and      # Вторая свеча медвежья
        latest['open'] <= previous['close'] and   # Вторая свеча внутри тела первой
        latest['close'] >= previous['open']):
        return "Bearish Harami"
    
    # Piercing Line (бычий паттерн)
    if (previous['close'] < previous['open'] and  # Первая свеча медвежья
        latest['close'] > latest['open'] and      # Вторая свеча бычья
        latest['open'] < previous['low'] and      # Открытие ниже минимума первой
        latest['close'] > (previous['open'] + previous['close']) / 2):  # Закрытие выше середины первой
        return "Piercing Line"
    
    # Dark Pool Cover (медвежий паттерн)
    if (previous['close'] > previous['open'] and  # Первая свеча бычья
        latest['close'] < latest['open'] and      # Вторая свеча медвежья
        latest['open'] > previous['high'] and     # Открытие выше максимума первой
        latest['close'] < (previous['open'] + previous['close']) / 2):  # Закрытие ниже середины первой
        return "Dark Pool Cover"
    
    # Three White Soldiers (бычий паттерн, 3 свечи)
    if len(df) >= 3:
        third = df.iloc[-1]
        second = df.iloc[-2]
        first = df.iloc[-3]
        shadow_third = (third['high'] - third['close']) if third['close'] > third['open'] else (third['high'] - third['open'])
        shadow_second = (second['high'] - second['close']) if second['close'] > second['open'] else (second['high'] - second['open'])
        shadow_first = (first['high'] - first['close']) if first['close'] > first['open'] else (first['high'] - first['open'])
        body_third = abs(third['close'] - third['open'])
        body_second = abs(second['close'] - second['open'])
        body_first = abs(first['close'] - first['open'])
        if (first['close'] > first['open'] and  # Все три свечи бычьи
            second['close'] > second['open'] and
            third['close'] > third['open'] and
            second['close'] > first['close'] and  # Каждая закрывается выше предыдущей
            third['close'] > second['close'] and
            shadow_first / body_first < 0.1 and   # Маленькие верхние тени
            shadow_second / body_second < 0.1 and
            shadow_third / body_third < 0.1):
            return "Three White Soldiers"
    
    # Three Black Crows (медвежий паттерн, 3 свечи)
    if len(df) >= 3:
        third = df.iloc[-1]
        second = df.iloc[-2]
        first = df.iloc[-3]
        shadow_third = (third['open'] - third['low']) if third['close'] < third['open'] else (third['close'] - third['low'])
        shadow_second = (second['open'] - second['low']) if second['close'] < second['open'] else (second['close'] - second['low'])
        shadow_first = (first['open'] - first['low']) if first['close'] < first['open'] else (first['close'] - first['low'])
        body_third = abs(third['close'] - third['open'])
        body_second = abs(second['close'] - second['open'])
        body_first = abs(first['close'] - first['open'])
        if (first['close'] < first['open'] and  # Все три свечи медвежьи
            second['close'] < second['open'] and
            third['close'] < third['open'] and
            second['close'] < first['close'] and  # Каждая закрывается ниже предыдущей
            third['close'] < second['close'] and
            shadow_first / body_first < 0.1 and   # Маленькие нижние тени
            shadow_second / body_second < 0.1 and
            shadow_third / body_third < 0.1):
            return "Three Black Crows"
    
    # Bullish Kicker (бычий паттерн)
    if (previous['close'] < previous['open'] and  # Первая свеча медвежья
        latest['close'] > latest['open'] and      # Вторая свеча бычья
        latest['open'] > previous['close']):      # Гэп вверх
        return "Bullish Kicker"
    
    # Bearish Kicker (медвежий паттерн)
    if (previous['close'] > previous['open'] and  # Первая свеча бычья
        latest['close'] < latest['open'] and      # Вторая свеча медвежья
        latest['open'] < previous['close']):      # Гэп вниз
        return "Bearish Kicker"
    
    # Inverted Hammer (бычий паттерн)
    if (upper_shadow >= 2 * body_latest and body_latest / range_latest < 0.2 and
        (latest['low'] - min(latest['open'], latest['close'])) < 0.1 * body_latest):
        return "Inverted Hammer"
    
    # Hanging Man (медвежий паттерн, после роста)
    if len(df) >= 3:
        prev_trend = df.iloc[-3:-1]['close'].mean() < df.iloc[-3:-1]['open'].mean()  # Проверяем предыдущий тренд
        if (lower_shadow >= 2 * body_latest and body_latest / range_latest < 0.2 and
            (max(latest['open'], latest['close']) - latest['high']) < 0.1 * body_latest and
            not prev_trend):  # Подтверждаем, что до этого был восходящий тренд
            return "Hanging Man"
    
    return None

# Функция для расчета индикаторов RSI, EMA, ATR и MACD
def calculate_indicators(df):
    """Расчет индикаторов с улучшенной валидацией данных."""
    try:
        if len(df) < 26:
            logging.error(f"Недостаточно данных для расчета индикаторов: {len(df)} свечей")
            return None, None, None, None, None, None, None
        
        # Проверка на пропущенные значения
        if df['close'].isna().any() or df['high'].isna().any() or df['low'].isna().any():
            logging.error("Обнаружены пропущенные значения в данных")
            return None, None, None, None, None, None, None
        
        # Фильтрация и подготовка данных
        df = df[(df['close'] > 0) & (df['high'] > 0) & (df['low'] > 0)].copy()
        if len(df) < 26:
            logging.error("После фильтрации данных осталось недостаточно свечей")
            return None, None, None, None, None, None, None
        
        df = df.reset_index(drop=True)
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        
        # Расчет индикаторов
        rsi_series = RSIIndicator(close=df['close'], window=14).rsi()
        ema_fast_series = EMAIndicator(close=df['close'], window=12).ema_indicator()
        ema_slow_series = EMAIndicator(close=df['close'], window=26).ema_indicator()
        atr_series = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()
        macd_obj = MACD(close=df['close'], window_slow=26, window_fast=12, window_sign=9)
        macd_series = macd_obj.macd()
        signal_series = macd_obj.macd_signal()
        
        # Проверка валидности результатов
        indicators = {
            'RSI': rsi_series,
            'EMA12': ema_fast_series,
            'EMA26': ema_slow_series,
            'ATR': atr_series,
            'MACD': macd_series,
            'Signal': signal_series
        }
        
        # Проверка каждого индикатора
        for name, series in indicators.items():
            if series.isna().all() or len(series.dropna()) == 0:
                logging.error(f"Ошибка: индикатор {name} пуст или содержит только NaN")
                return None, None, None, None, None, None, None
            if pd.isna(series.iloc[-1]):
                logging.error(f"Ошибка: последнее значение индикатора {name} отсутствует")
                return None, None, None, None, None, None, None
        
        # Получение последних значений
        rsi_value = float(rsi_series.iloc[-1])
        ema_fast_value = float(ema_fast_series.iloc[-1])
        ema_slow_value = float(ema_slow_series.iloc[-1])
        atr_value = float(atr_series.iloc[-1])
        macd_line = float(macd_series.iloc[-1])
        signal_line = float(signal_series.iloc[-1])
        candle_pattern = detect_candle_patterns(df)
        
        # Формирование сводки индикаторов
        indicator_summary = (
            f"📊 Текущие индикаторы:\n"
            f"  RSI: {rsi_value:.2f}\n"
            f"  EMA12: {ema_fast_value:.2f}\n"
            f"  EMA26: {ema_slow_value:.2f}\n"
            f"  ATR: {atr_value:.2f}\n"
            f"  MACD: {macd_line:.2f}\n"
            f"  Signal: {signal_line:.2f}\n"
            f"  Свечной паттерн: {candle_pattern if candle_pattern else 'Не обнаружен'}"
        )
        logging.info(indicator_summary)
        
        return rsi_value, ema_fast_value, ema_slow_value, atr_value, macd_line, signal_line, candle_pattern
    except Exception as e:
        logging.error(f"Ошибка при расчете индикаторов: {e}")
        return None, None, None, None, None, None, None

# Функция для генерации торговых сигналов
def generate_signal(rsi, ema_fast, ema_slow, atr, macd_line, signal_line, candle_pattern):
    """Генерация торговых сигналов с расширенным набором паттернов."""
    try:
        if any(v is None for v in [rsi, ema_fast, ema_slow, atr, candle_pattern]):
            logging.warning("Не удалось сгенерировать сигнал: одно из значений индикаторов равно None")
            return None
        
        # Логирование входных данных для отладки
        logging.info(
            f"Генерация сигнала:\n"
            f"  RSI: {rsi:.2f}\n"
            f"  EMA12: {ema_fast:.2f}\n"
            f"  EMA26: {ema_slow:.2f}\n"
            f"  Candle Pattern: {candle_pattern if candle_pattern else 'Не обнаружен'}"
        )
        
        # Бычьи паттерны
        bullish_patterns = [
            "Bullish Engulfing", "Doji", "Hammer", "Morning Star",
            "Bullish Harami", "Piercing Line", "Three White Soldiers",
            "Bullish Kicker", "Inverted Hammer"
        ]
        
        # Медвежьи паттерны
        bearish_patterns = [
            "Bearish Engulfing", "Doji", "Shooting Star",
            "Bearish Harami", "Dark Pool Cover", "Three Black Crows",
            "Bearish Kicker", "Hanging Man"
        ]
        
        # Условия для покупки: RSI < 55, EMA12 > EMA26, бычьи свечные паттерны
        if (rsi < 55 and ema_fast > ema_slow and candle_pattern in bullish_patterns):
            logging.info("Сгенерирован сигнал Buy: все условия выполнены")
            return "Buy"
        
        # Условия для продажи: RSI > 55, EMA12 < EMA26, медвежьи свечные паттерны
        elif (rsi > 55 and ema_fast < ema_slow and candle_pattern in bearish_patterns):
            logging.info("Сгенерирован сигнал Sell: все условия выполнены")
            return "Sell"
        
        # Логируем причину отсутствия сигнала
        if rsi >= 55:
            logging.info("Нет сигнала Buy: RSI >= 55")
        elif rsi <= 55:
            logging.info("Нет сигнала Sell: RSI <= 55")
        if ema_fast <= ema_slow:
            logging.info("Нет сигнала Buy: EMA12 <= EMA26")
        elif ema_fast >= ema_slow:
            logging.info("Нет сигнала Sell: EMA12 >= EMA26")
        if candle_pattern not in bullish_patterns:
            logging.info("Нет сигнала Buy: отсутствует нужный свечной паттерн")
        if candle_pattern not in bearish_patterns:
            logging.info("Нет сигнала Sell: отсутствует нужный свечной паттерн")
        
        return None
    except Exception as e:
        logging.error(f"Ошибка при генерации сигнала: {e}")
        return None

def generate_signal_test(rsi, ema_fast, ema_slow, atr, macd_line, signal_line, candle_pattern):
    """Тестовая версия генерации сигналов с упрощенными условиями."""
    try:
        if any(v is None for v in [rsi, ema_fast, ema_slow, atr, macd_line, signal_line, candle_pattern]):
            logging.warning("Не удалось сгенерировать тестовый сигнал: одно из значений индикаторов равно None")
            return None
        
        logging.info(
            f"Генерация тестового сигнала:\n"
            f"  RSI: {rsi:.2f}\n"
            f"  EMA12: {ema_fast:.2f}\n"
            f"  EMA26: {ema_slow:.2f}\n"
            f"  MACD: {macd_line:.2f}\n"
            f"  Signal: {signal_line:.2f}\n"
            f"  Candle Pattern: {candle_pattern if candle_pattern else 'Не обнаружен'}"
        )
        
        # Упрощенные условия для тестирования
        if rsi < 60 and ema_fast > ema_slow:
            logging.info("Тестовый сигнал Buy: RSI < 60 и EMA12 > EMA26")
            return "Buy"
        elif rsi > 60 and ema_fast < ema_slow:
            logging.info("Тестовый сигнал Sell: RSI > 60 и EMA12 < EMA26")
            return "Sell"
        else:
            if rsi >= 60:
                logging.info("Нет тестового сигнала Buy: RSI >= 60")
            elif rsi <= 60:
                logging.info("Нет тестового сигнала Sell: RSI <= 60")
            if ema_fast <= ema_slow:
                logging.info("Нет тестового сигнала Buy: EMA12 <= EMA26")
            elif ema_fast >= ema_slow:
                logging.info("Нет тестового сигнала Sell: EMA12 >= EMA26")
            return None
    except Exception as e:
        logging.error(f"Ошибка при генерации тестового сигнала: {e}")
        return None

# Функция для проверки открытых позиций
def check_position(symbol=None):
    if symbol is None:
        symbol = SELECTED_SYMBOL
    try:
        response = session.get_position(category="linear", symbol=symbol)
        position = response['result']['list']
        if not position:
            return 0, None, 0
        for pos in position:
            if pos['symbol'] == symbol and pos['side'] in ["Buy", "Sell"]:
                qty = float(pos['size'])
                side = pos['side']
                entry_price = float(pos['entryPrice'])
                return qty, side, entry_price
        return 0, None, 0
    except Exception as e:
        logging.error(f"Ошибка при проверке позиции для {symbol}: {e}")
        return 0, None, 0

# Функция для логирования сделок
def log_trade(action, side, price, qty, status="executed", pnl=0.0):
    trade = {
        "timestamp": str(pd.Timestamp.now()),
        "action": action,
        "side": side,
        "price": price,
        "qty": qty,
        "status": status,
        "symbol": SYMBOL,
        "pnl": pnl,
        "invested": price * qty if action == "open" else 0.0
    }
    try:
        with open(TRADES_LOG_FILE, 'a') as f:
            f.write(json.dumps(trade) + "\n")
        logging.info(f"Сделка записана: {trade}")
    except Exception as e:
        logging.error(f"Ошибка при записи в лог сделок: {e}")

# Функция для размещения ордера
def place_order(side, price, qty, symbol=None):
    if symbol is None:
        symbol = SELECTED_SYMBOL
    try:
        order = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=qty,
            reduceOnly=False
        )
        if order['retCode'] == 0:
            invested = qty * price
            positions[symbol] = {"side": side, "qty": qty, "entry_price": price, "invested": invested}
            message = f"✅ Ордер размещён: {side} {symbol}, Объём: {qty}, Цена: {price} USDT"
            print(message)
            send_telegram_message(message)
        else:
            logging.error(f"Ошибка при размещении ордера: {order['retMsg']}")
            send_telegram_message(f"❌ Ошибка при размещении ордера: {order['retMsg']}")
    except Exception as e:
        logging.error(f"Ошибка при размещении ордера для {symbol}: {e}")
        send_telegram_message(f"❌ Ошибка при размещении ордера: {e}")

# Функция для закрытия позиции
def close_position(side, qty, current_price, entry_price, symbol=None):
    global total_pnl
    if symbol is None:
        symbol = SELECTED_SYMBOL
    try:
        close_side = "Sell" if side == "Buy" else "Buy"
        order = session.place_order(
            category="linear",
            symbol=symbol,
            side=close_side,
            orderType="Market",
            qty=qty,
            reduceOnly=True
        )
        if order['retCode'] == 0:
            pnl = (current_price - entry_price) * qty if side == "Buy" else (entry_price - current_price) * qty
            total_pnl += pnl
            positions.pop(symbol, None)
            message = f"🔒 Позиция закрыта: {side} {symbol}, Объём: {qty}, PnL: {pnl:.2f} USDT"
            print(message)
            send_telegram_message(message)
        else:
            logging.error(f"Ошибка при закрытии позиции: {order['retMsg']}")
            send_telegram_message(f"❌ Ошибка при закрытии позиции: {order['retMsg']}")
    except Exception as e:
        logging.error(f"Ошибка при закрытии позиции для {symbol}: {e}")
        send_telegram_message(f"❌ Ошибка при закрытии позиции: {e}")

def get_telegram_updates():
    """Получение обновлений от Telegram API."""
    global TELEGRAM_OFFSET
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"timeout": 60, "offset": TELEGRAM_OFFSET}
        response = requests.get(url, params=params, timeout=65)
        data = response.json()
        
        logging.info(f"Сырые данные ответа Telegram: {data}")
        
        if not data.get("ok"):
            logging.error(f"Ошибка при получении обновлений Telegram: {data.get('description', 'Неизвестная ошибка')}")
            return []
        
        updates = data.get("result", [])
        logging.info(f"Получены обновления Telegram: {updates}")
        
        if updates:
            TELEGRAM_OFFSET = max(update["update_id"] for update in updates) + 1
        
        # Обрабатываем сообщения и callback-запросы
        processed_updates = []
        for update in updates:
            if "message" in update:
                processed_updates.append(update)
            elif "callback_query" in update:
                logging.info(f"Получен callback: {update['callback_query']['data']}")
                handle_telegram_callback(update["callback_query"])
        
        return processed_updates
    except Exception as e:
        logging.error(f"Ошибка при получении обновлений Telegram: {e}")
        return []

def close_all_positions():
    """Закрытие всех открытых позиций."""
    try:
        # Проверяем текущие позиции
        position_qty, position_side, entry_price = check_position()
        if position_qty == 0:
            message = "📋 Нет открытых позиций для закрытия."
            print(message)
            send_telegram_message(message)
            logging.info(message)
            return
        
        # Получаем текущую цену для расчета PnL
        current_price = float(session.get_tickers(category="linear", symbol=SYMBOL)['result']['list'][0]['lastPrice'])
        close_position(position_side, position_qty, current_price, entry_price)
        message = f"✅ Все позиции закрыты.\n  Символ: {SYMBOL}\n  Цена закрытия: {current_price} USDT\n  Общий PnL: {total_pnl:.2f} USDT"
        print(message)
        send_telegram_message(message)
        logging.info(message)
    except Exception as e:
        error_msg = f"⚠️ Ошибка при закрытии всех позиций: {e}"
        print(error_msg)
        send_telegram_message(error_msg)
        logging.error(error_msg)

def show_candle_chart():
    """Отображение свечного графика последних 5 свечей."""
    try:
        df = get_klines()
        if df is None:
            error_msg = "⚠️ Не удалось получить данные свечей для построения графика"
            print(error_msg)
            send_telegram_message(error_msg)
            return
        
        # Проверка актуальности данных
        latest_time = df['timestamp'].iloc[-1]
        time_diff = (pd.Timestamp.now() - latest_time).total_seconds()
        if time_diff > 10800:  # 3 часа
            warning_msg = f"⚠️ Данные устарели на {time_diff/3600:.1f} часов"
            logging.warning(warning_msg)
            send_telegram_message(warning_msg)
        
        latest_5 = df.tail(5)
        
        # Создаем временный HTML файл
        temp_dir = tempfile.gettempdir()
        chart_file = Path(temp_dir) / "candle_chart.html"
        
        # Формируем данные для графика
        chart_data = []
        for _, row in latest_5.iterrows():
            chart_data.append({
                'x': row['timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
                'o': float(row['open']),
                'h': float(row['high']),
                'l': float(row['low']),
                'c': float(row['close'])
            })
        
        # Создаем HTML с графиком
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>BTCUSDT 5m Chart</title>
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
            <script src="https://cdn.jsdelivr.net/npm/chartjs-chart-financial"></script>
            <style>
                body {{ background-color: #1a1a1a; color: #ffffff; }}
                canvas {{ background-color: #2a2a2a; }}
            </style>
        </head>
        <body>
            <canvas id="candleChart" width="800" height="400"></canvas>
            <script>
                const ctx = document.getElementById('candleChart').getContext('2d');
                const data = {json.dumps(chart_data)};
                new Chart(ctx, {{
                    type: 'candlestick',
                    data: {{
                        datasets: [{{
                            label: 'BTCUSDT 5m',
                            data: data.map(d => ({{
                                x: new Date(d.x).getTime(),
                                o: d.o,
                                h: d.h,
                                l: d.l,
                                c: d.c
                            }}))
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        scales: {{
                            x: {{
                                type: 'time',
                                time: {{
                                    unit: 'minute',
                                    displayFormats: {{
                                        minute: 'HH:mm'
                                    }}
                                }}
                            }},
                            y: {{
                                position: 'right'
                            }}
                        }},
                        plugins: {{
                            legend: {{
                                display: true,
                                position: 'top'
                            }},
                            tooltip: {{
                                callbacks: {{
                                    label: function(context) {{
                                        const candle = context.dataset.data[context.dataIndex];
                                        return [
                                            `Open: $${{candle.o.toFixed(2)}}`,
                                            `High: $${{candle.h.toFixed(2)}}`,
                                            `Low: $${{candle.l.toFixed(2)}}`,
                                            `Close: $${{candle.c.toFixed(2)}}`
                                        ];
                                    }}
                                }}
                            }}
                        }}
                    }}
                }});
            </script>
        </body>
        </html>
        """
        
        with open(chart_file, 'w') as f:
            f.write(html_content)
        
        # Открываем график в браузере
        webbrowser.open(f'file://{chart_file}')
        message = "📊 График свечей открыт в браузере!"
        print(message)
        send_telegram_message(message)
    except Exception as e:
        error_msg = f"⚠️ Ошибка при построении графика: {e}"
        print(error_msg)
        send_telegram_message(error_msg)
        logging.error(error_msg)

# Функция для расчёта волатильности одной монеты
def calculate_volatility(symbol):
    try:
        # Запрашиваем 1-часовые свечи за последние 24 часа (24 свечи)
        klines = session.get_kline(
            category="linear",
            symbol=symbol,
            interval="60",  # 1 час
            limit=24  # 24 часа
        )['result']['list']
        
        if not klines:
            return symbol, 0.0
        
        # Преобразуем в DataFrame
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        
        # Рассчитываем волатильность: ((High - Low) / Low) * 100
        df['volatility'] = ((df['high'] - df['low']) / df['low']) * 100
        avg_volatility = df['volatility'].mean()
        
        return symbol, avg_volatility
    except Exception as e:
        logging.error(f"Ошибка при расчёте волатильности для {symbol}: {e}")
        return symbol, 0.0

def get_top_volatile_coins():
    """
    Возвращает список из 5 самых волатильных монет за последние 24 часа с кэшированием.
    Returns:
        List of tuples: [(symbol, volatility), ...]
    """
    cache_file = "volatile_coins_cache.json"
    current_time = int(time.time())

    # Проверяем кэш
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as f:
                cache = json.load(f)
            if current_time - cache["timestamp"] < 3600:  # Кэш действителен 1 час
                logging.info(f"Используется кэш: {cache['timestamp']}")
                return cache["data"]
        except Exception as e:
            logging.error(f"Ошибка при чтении кэша: {e}")

    logging.info("Начало анализа волатильных монет")
    start_time = time.time()
    try:
        # Получаем список всех фьючерсных пар
        tickers = session.get_tickers(category="linear")['result']['list']
        
        # Сортируем по объёму торгов (turnover24h) и берём топ-50
        tickers.sort(key=lambda x: float(x.get('turnover24h', 0)), reverse=True)
        top_tickers = tickers[:50]
        symbols = [ticker['symbol'] for ticker in top_tickers]
        
        # Параллельно рассчитываем волатильность
        volatility_data = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_symbol = {executor.submit(calculate_volatility, symbol): symbol for symbol in symbols}
            for future in as_completed(future_to_symbol):
                symbol, volatility = future.result()
                if volatility > 0:  # Исключаем пары с нулевой волатильностью
                    volatility_data.append((symbol, volatility))
        
        # Сортируем по убыванию волатильности и берём топ-5
        volatility_data.sort(key=lambda x: x[1], reverse=True)
        top_5 = volatility_data[:5]
        
        # Сохраняем результат в кэш
        cache_data = {
            "timestamp": current_time,
            "data": top_5
        }
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)
        
        elapsed_time = time.time() - start_time
        logging.info(f"Завершён анализ волатильных монет за {elapsed_time:.2f} секунд")
        return top_5
    except Exception as e:
        logging.error(f"Ошибка при получении волатильных монет: {e}")
        return []

def send_status():
    """Отправка текущего статуса бота."""
    global SELECTED_SYMBOL
    try:
        if SELECTED_SYMBOL is None:
            logging.info("Статус отправлен для SELECTED_SYMBOL: None")
            send_telegram_message("📊 Статус бота:\n  Монета не выбрана. Выберите монету через меню.")
            return

        current_balance = check_balance()
        position_qty, position_side, entry_price = check_position(symbol=SELECTED_SYMBOL)
        current_price = float(session.get_tickers(category="linear", symbol=SELECTED_SYMBOL)['result']['list'][0]['lastPrice']) if SELECTED_SYMBOL else 0.0
        invested_total = sum(pos["invested"] for pos in positions.values()) if positions else 0
        unrealized_pnl = 0
        if position_qty > 0:
            unrealized_pnl = (current_price - entry_price) * position_qty if position_side == "Buy" else (entry_price - current_price) * position_qty
        total_pnl_current = total_pnl + unrealized_pnl

        status_message = (
            f"📊 Статус бота:\n"
            f"  Баланс: {current_balance:.2f} USDT\n"
            f"  Общий PnL: {total_pnl:.2f} USDT\n"
            f"  Открытых позиций: {position_qty if position_qty > 0 else 0}\n"
            f"  Текущая цена: {current_price:.2f} USDT"
        )
        logging.info(f"Статус отправлен для SELECTED_SYMBOL: {SELECTED_SYMBOL}")
        send_telegram_message(status_message)
    except Exception as e:
        logging.error(f"Ошибка при отправке статуса: {e}")
        send_telegram_message(f"⚠️ Ошибка при получении статуса: {e}")

def handle_telegram_commands(message):
    """Обработка команд из Telegram."""
    global SELECTED_SYMBOL, TOP_VOLATILE_COINS
    logging.info(f"Обработка команды Telegram: {message}")
    
    if message == "/closeall":
        if SELECTED_SYMBOL is None:
            return True  # Убираем уведомление, так как меню уже отображено
        close_all_positions()
        return True
    elif message == "/status":
        send_status()
        return True
    elif message == "/showchart":
        if SELECTED_SYMBOL is None:
            return True  # Убираем уведомление, так как меню уже отображено
        show_candle_chart()
        return True
    elif message == "/refreshcoins":
        TOP_VOLATILE_COINS = get_top_volatile_coins()
        if TOP_VOLATILE_COINS:
            coin_list = "\n".join([f"{i+1}. {symbol} - {volatility:.2f}%" for i, (symbol, volatility) in enumerate(TOP_VOLATILE_COINS)])
            selection_message = (
                f"📈 Топ-5 волатильных монет:\n"
                f"{coin_list}\n"
                f"Выберите монету для анализа:"
            )
            reply_markup = {
                "inline_keyboard": [
                    [{"text": symbol, "callback_data": f"coin_{symbol}"}] for symbol, _ in TOP_VOLATILE_COINS
                ]
            }
            send_telegram_message(selection_message, reply_markup=reply_markup)
        else:
            send_telegram_message("⚠️ Не удалось получить список волатильных монет.")
        return True
    elif message.startswith("/selectcoin"):
        try:
            parts = message.split()
            if len(parts) != 2:
                # Если символ не указан, показываем список волатильных монет
                if TOP_VOLATILE_COINS:
                    coin_list = "\n".join([f"{i+1}. {symbol} - {volatility:.2f}%" for i, (symbol, volatility) in enumerate(TOP_VOLATILE_COINS)])
                    selection_message = (
                        f"📈 Топ-5 волатильных монет:\n"
                        f"{coin_list}\n"
                        f"Выберите монету для анализа:"
                    )
                    reply_markup = {
                        "inline_keyboard": [
                            [{"text": symbol, "callback_data": f"coin_{symbol}"}] for symbol, _ in TOP_VOLATILE_COINS
                        ]
                    }
                    send_telegram_message(selection_message, reply_markup=reply_markup)
                else:
                    send_telegram_message("⚠️ Список волатильных монет пуст. Используйте /refreshcoins для обновления.")
                return True
            
            symbol = parts[1].upper()
            tickers = session.get_tickers(category="linear")['result']['list']
            available_symbols = [ticker['symbol'] for ticker in tickers]
            
            if symbol in available_symbols:
                SELECTED_SYMBOL = symbol
                send_telegram_message(
                    f"✅ Монета для торговли выбрана: {symbol}\n"
                    f"ℹ️ Вы выбрали монету через команду. Рекомендуется использовать меню волатильных монет для выбора с анализом."
                )
            else:
                send_telegram_message(f"❌ Монета {symbol} недоступна. Выберите из списка волатильных монет.")
            return True
        except Exception as e:
            logging.error(f"Ошибка при обработке команды /selectcoin: {e}")
            send_telegram_message(f"❌ Ошибка при выборе монеты: {e}")
            return True
    return False

# Основная торговая функция
def trading_loop():
    global initial_balance, total_pnl, positions, SELECTED_SYMBOL, ws, TOP_VOLATILE_COINS
    initial_balance = check_balance()
    if initial_balance < POSITION_SIZE * 10000:
        error_msg = "⚠️ Недостаточно средств на балансе. Пополните USDT на testnet или переведите в Derivatives."
        logging.error(error_msg)
        send_telegram_message(error_msg)
        return

    # Отправляем сообщение о доступных командах
    commands_message = (
        "🤖 Бот запущен!\n"
        "Доступные команды:\n"
        "/closeall - Закрыть все позиции\n"
        "/status - Показать текущий статус\n"
        "/showchart - Показать график свечей\n"
        "/refreshcoins - Обновить список волатильных монет"
    )
    send_telegram_message(commands_message)

    # Получаем топ-5 волатильных монет
    TOP_VOLATILE_COINS = get_top_volatile_coins()
    if TOP_VOLATILE_COINS:
        coin_list = "\n".join([f"{i+1}. {symbol} - {volatility:.2f}%" for i, (symbol, volatility) in enumerate(TOP_VOLATILE_COINS)])
        selection_message = (
            f"📈 Топ-5 волатильных монет:\n"
            f"{coin_list}\n"
            f"Выберите монету для анализа:"
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": symbol, "callback_data": f"coin_{symbol}"}] for symbol, _ in TOP_VOLATILE_COINS
            ]
        }
        send_telegram_message(selection_message, reply_markup=reply_markup)
    else:
        send_telegram_message("⚠️ Не удалось получить список волатильных монет. Используйте /refreshcoins для повторной попытки.")

    last_update = 0
    try:
        while True:
            current_time = time.time()
            if current_time - last_update >= UPDATE_INTERVAL:
                # Проверяем, выбрана ли монета
                if SELECTED_SYMBOL is None:
                    last_update = current_time
                    continue

                if ws is None:
                    ws = start_websocket(symbol=SELECTED_SYMBOL)
                    if ws is None:
                        error_msg = "⚠️ Не удалось запустить WebSocket."
                        print(error_msg)
                        send_telegram_message(error_msg)
                        last_update = current_time
                        continue

                df = get_klines(symbol=SELECTED_SYMBOL)
                if df is not None:
                    latest_5 = df.tail(5)
                    candles_summary = "\n📅 Последние 5 свечей (5-минутный таймфрейм):\n" + latest_5.to_string(index=False)
                    print(candles_summary)

                    rsi, ema_fast, ema_slow, atr, macd_line, signal_line, candle_pattern = calculate_indicators(df)
                    if rsi is not None and ema_fast is not None and ema_slow is not None and \
                       atr is not None and macd_line is not None and signal_line is not None:
                        adjusted_rsi_threshold = ai_assist(df, base_rsi_threshold=55)
                        indicator_summary = (
                            f"\n📊 Текущие индикаторы ({SELECTED_SYMBOL}):\n"
                            f"  RSI: {rsi:.2f} (скорректированный порог: {adjusted_rsi_threshold})\n"
                            f"  EMA12: {ema_fast:.2f}\n"
                            f"  EMA26: {ema_slow:.2f}\n"
                            f"  ATR: {atr:.2f}\n"
                            f"  MACD: {macd_line:.2f}\n"
                            f"  Signal: {signal_line:.2f}\n"
                            f"  Свечной паттерн: {candle_pattern if candle_pattern else 'Не обнаружен'}"
                        )
                        print(indicator_summary)
                        latest_indicators["rsi"] = rsi
                        latest_indicators["ema_fast"] = ema_fast
                        latest_indicators["ema_slow"] = ema_slow
                        latest_indicators["atr"] = atr
                        latest_indicators["macd"] = macd_line
                        latest_indicators["signal_line"] = signal_line
                        latest_indicators["candle_pattern"] = candle_pattern
                        signal = generate_signal(rsi, ema_fast, ema_slow, atr, macd_line, signal_line, candle_pattern)
                        latest_indicators["signal"] = signal
                        signal_msg = f"📡 Сигнал: {signal if signal else 'Нет сигнала'}"
                        print(signal_msg)
                        send_telegram_message(indicator_summary + "\n" + signal_msg + "\n" + position_summary + status_summary)
                    else:
                        error_msg = "⚠️ Не удалось рассчитать индикаторы"
                        print(error_msg)
                        send_telegram_message(error_msg)

                    position_qty, position_side, entry_price = check_position(symbol=SELECTED_SYMBOL)
                    current_price = float(session.get_tickers(category="linear", symbol=SELECTED_SYMBOL)['result']['list'][0]['lastPrice'])
                    invested_total = sum(pos["invested"] for pos in positions.values()) if positions else 0
                    unrealized_pnl = 0
                    if position_qty > 0:
                        unrealized_pnl = (current_price - entry_price) * position_qty if position_side == "Buy" else (entry_price - current_price) * position_qty
                        position_summary = (
                            f"\n📋 Открытая позиция ({SELECTED_SYMBOL}):\n"
                            f"  Символ: {SELECTED_SYMBOL}\n"
                            f"  Сторона: {position_side}\n"
                            f"  Цена входа: {entry_price} USDT\n"
                            f"  Текущая цена: {current_price} USDT\n"
                            f"  Объем: {position_qty}\n"
                            f"  Вложено: {invested_total:.2f} USDT\n"
                            f"  Нереализованный PnL: {unrealized_pnl:.2f} USDT"
                        )
                        print(position_summary)
                    else:
                        position_summary = f"\n📋 Открытых позиций нет ({SELECTED_SYMBOL})."
                        print(position_summary)

                    total_pnl_current = total_pnl + unrealized_pnl
                    current_balance = check_balance()
                    status_summary = (
                        f"\n💰 Статус счета:\n"
                        f"  Текущий баланс: {current_balance:.2f} USDT\n"
                        f"  Вложено в позиции: {invested_total:.2f} USDT\n"
                        f"  Текущий PnL: {total_pnl_current:.2f} USDT\n"
                        f"  Общий PnL: {total_pnl:.2f} USDT"
                    )
                    print(status_summary)

                    if position_qty > 0:
                        if total_pnl_current < 0 and abs(total_pnl_current) > initial_balance * MAX_LOSS_PCT:
                            error_msg = f"⚠️ Достигнут максимальный убыток {MAX_LOSS_PCT*100}%: {abs(total_pnl_current):.2f} USDT"
                            logging.error(error_msg)
                            close_position(position_side, position_qty, current_price, entry_price, symbol=SELECTED_SYMBOL)
                            send_telegram_message(error_msg)
                            break
                        if position_side == "Buy":
                            if current_price <= entry_price * (1 - STOP_LOSS_PCT) or current_price >= entry_price * (1 + TAKE_PROFIT_PCT):
                                close_position(position_side, position_qty, current_price, entry_price, symbol=SELECTED_SYMBOL)
                        else:
                            if current_price >= entry_price * (1 + STOP_LOSS_PCT) or current_price <= entry_price * (1 - TAKE_PROFIT_PCT):
                                close_position(position_side, position_qty, current_price, entry_price, symbol=SELECTED_SYMBOL)

                    if signal and position_qty == 0:
                        if check_liquidity(current_price, POSITION_SIZE, symbol=SELECTED_SYMBOL):
                            place_order(signal, current_price, POSITION_SIZE, symbol=SELECTED_SYMBOL)
                        else:
                            liquidity_msg = "⚠️ Ордер не размещен: недостаточно ликвидности"
                            print(liquidity_msg)
                            send_telegram_message(liquidity_msg)
                    else:
                        status_msg = "Ордер не размещен: нет сигнала или открыта позиция"
                        print(status_msg)
                else:
                    error_msg = f"⚠️ Не удалось получить свечи для {SELECTED_SYMBOL}"
                    print(error_msg)
                    send_telegram_message(error_msg)
                last_update = current_time

            # Проверка новых сообщений и callback-запросов в Telegram
            updates = get_telegram_updates()
            logging.info(f"Проверка новых сообщений Telegram: {updates}")
            if updates:
                for update in updates:
                    if 'message' in update and 'text' in update['message']:
                        message = update['message']['text']
                        if handle_telegram_commands(message):
                            continue

            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Программа остановлена пользователем")
        if ws is not None:
            ws.exit()

# Основная функция
def main():
    send_telegram_message("🔔 Бот запущен! Проверка уведомлений.")
    trading_loop()

def ai_assist(df, base_rsi_threshold=55):
    """
    Динамически корректирует пороговые значения RSI на основе волатильности (ATR).
    Args:
        df: DataFrame с данными свечей
        base_rsi_threshold: Базовый порог RSI (по умолчанию 55)
    Returns:
        Корректированный порог RSI
    """
    try:
        if len(df) < 14:
            logging.warning("Недостаточно данных для расчета ATR в ai_assist")
            return base_rsi_threshold
        
        atr_series = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()
        if atr_series.isna().all():
            logging.warning("ATR содержит NaN, используется базовый порог RSI")
            return base_rsi_threshold
        
        latest_atr = float(atr_series.iloc[-1])
        avg_atr = atr_series[-14:].mean()
        
        # Корректировка порога RSI
        if latest_atr > avg_atr * 1.2:  # Если ATR выше среднего на 20%
            adjusted_rsi = base_rsi_threshold - 5
            logging.info(f"Высокая волатильность (ATR: {latest_atr:.2f}, среднее: {avg_atr:.2f}), RSI порог снижен до {adjusted_rsi}")
            return max(30, adjusted_rsi)  # Не опускаем ниже 30
        elif latest_atr < avg_atr * 0.8:  # Если ATR ниже среднего на 20%
            adjusted_rsi = base_rsi_threshold + 5
            logging.info(f"Низкая волатильность (ATR: {latest_atr:.2f}, среднее: {avg_atr:.2f}), RSI порог увеличен до {adjusted_rsi}")
            return min(70, adjusted_rsi)  # Не поднимаем выше 70
        else:
            logging.info(f"Волатильность в норме (ATR: {latest_atr:.2f}, среднее: {avg_atr:.2f}), используется базовый RSI порог {base_rsi_threshold}")
            return base_rsi_threshold
    except Exception as e:
        logging.error(f"Ошибка в ai_assist: {e}")
        return base_rsi_threshold

# Новая функция для ИИ-анализа
def perform_ai_analysis(symbol):
    try:
        # Получаем данные свечей
        df = get_klines(symbol=symbol)
        if df is None or len(df) < 200:
            return f"⚠️ Недостаточно данных для анализа {symbol}"

        # Рассчитываем индикаторы
        rsi, ema_fast, ema_slow, atr, macd_line, signal_line, candle_pattern = calculate_indicators(df)
        if any(v is None for v in [rsi, ema_fast, ema_slow, atr]):
            return f"⚠️ Не удалось рассчитать индикаторы для {symbol}"

        # Формируем анализ
        rsi_status = "перепродан" if rsi < 30 else "перекуплен" if rsi > 70 else "нейтрально"
        ema_status = "выше" if ema_fast > ema_slow else "ниже"
        atr_avg = df.tail(14)['high'].astype(float).mean() - df.tail(14)['low'].astype(float).mean()
        atr_status = "высокая" if atr > atr_avg else "низкая"
        
        market_outlook = ""
        if rsi < 30 and ema_fast > ema_slow:
            market_outlook = "Рынок перепродан, возможен разворот вверх"
        elif rsi > 70 and ema_fast < ema_slow:
            market_outlook = "Рынок перекуплен, возможен разворот вниз"
        else:
            market_outlook = "Рынок нейтрален, ожидайте более чётких сигналов"

        analysis = (
            f"🤖 ИИ-анализ для *{symbol}*:\n"
            f"- RSI: {rsi:.2f} ({rsi_status})\n"
            f"- EMA: EMA12 ({ema_fast:.2f}) {ema_status} EMA26 ({ema_slow:.2f})\n"
            f"- ATR: {atr:.2f} ({atr_status} волатильность)\n"
            f"- Свечной паттерн: {candle_pattern if candle_pattern else 'Не обнаружен'}\n"
            f"**Вывод:** {market_outlook}"
        )

        # Формируем inline keyboard
        reply_markup = {
            "inline_keyboard": [[
                {"text": "Торговать", "callback_data": f"trade_{symbol}"},
                {"text": "Назад", "callback_data": "back"}
            ]]
        }
        send_telegram_message(analysis, reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"Ошибка при выполнении ИИ-анализа для {symbol}: {e}")
        send_telegram_message(f"⚠️ Ошибка при анализе {symbol}: {e}")

# Новая функция для обработки callback-запросов
def handle_telegram_callback(callback_query):
    """Обработка callback-запросов от Telegram."""
    callback_data = callback_query["data"]
    callback_id = callback_query["id"]
    logging.info(f"Обработка callback: {callback_data}")

    # Отправляем ответ на callback
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
        payload = {"callback_query_id": callback_id}
        requests.post(url, json=payload)
    except Exception as e:
        logging.error(f"Ошибка при отправке ответа на callback: {e}")

    if callback_data.startswith("coin_"):
        symbol = callback_data.replace("coin_", "")
        perform_ai_analysis(symbol)
    elif callback_data.startswith("trade_"):
        global SELECTED_SYMBOL
        symbol = callback_data.replace("trade_", "")
        SELECTED_SYMBOL = symbol
        send_telegram_message(f"✅ Монета для торговли выбрана: {symbol}")
    elif callback_data == "back":
        if TOP_VOLATILE_COINS:
            coin_list = "\n".join([f"{i+1}. {symbol} - {volatility:.2f}%" for i, (symbol, volatility) in enumerate(TOP_VOLATILE_COINS)])
            selection_message = (
                f"📈 Топ-5 волатильных монет:\n"
                f"{coin_list}\n"
                f"Выберите монету для анализа:"
            )
            reply_markup = {
                "inline_keyboard": [
                    [{"text": symbol, "callback_data": f"coin_{symbol}"}] for symbol, _ in TOP_VOLATILE_COINS
                ]
            }
            send_telegram_message(selection_message, reply_markup=reply_markup)
        else:
            send_telegram_message("⚠️ Список волатильных монет пуст. Используйте /refreshcoins для обновления.")

if __name__ == "__main__":
    main()