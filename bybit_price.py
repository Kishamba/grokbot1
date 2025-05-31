import os
import logging
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Загрузка API-ключей из файла .env
load_dotenv()
api_key = os.getenv('BYBIT_API_KEY')
api_secret = os.getenv('BYBIT_API_SECRET')

# Проверка наличия API-ключей
if not api_key or not api_secret:
    logging.error("API-ключи не найдены в файле .env")
    exit(1)

# Подключение к Bybit testnet
try:
    session = HTTP(
        testnet=True,  # Используем testnet
        api_key=api_key,
        api_secret=api_secret
    )
    logging.info("Успешное подключение к Bybit testnet")
except Exception as e:
    logging.error(f"Ошибка подключения к Bybit: {e}")
    exit(1)

# Функция для получения текущей цены BTCUSDT
def get_current_price(symbol="BTCUSDT"):
    try:
        ticker = session.get_tickers(category="linear", symbol=symbol)
        price = float(ticker['result']['list'][0]['lastPrice'])
        logging.info(f"Текущая цена {symbol}: {price} USDT")
        return price
    except Exception as e:
        logging.error(f"Ошибка при получении цены {symbol}: {e}")
        return None

# Основная функция
def main():
    price = get_current_price()
    if price:
        print(f"Текущая цена BTCUSDT: {price} USDT")
    else:
        print("Не удалось получить цену")

if __name__ == "__main__":
    main() 