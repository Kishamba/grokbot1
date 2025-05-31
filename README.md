# Bybit Scalping Bot

A cryptocurrency trading bot that operates on the Bybit testnet, featuring technical analysis, AI-assisted trading decisions, and Telegram integration.

## Features

- Real-time price monitoring via WebSocket
- Technical indicators (RSI, EMA, MACD, ATR)
- Candlestick pattern detection
- AI-assisted trading decisions
- Telegram integration with interactive buttons
- Risk management with stop-loss and take-profit
- Position tracking and PnL calculation

## Requirements

- Python 3.8-3.12 (Python 3.13 not supported)
- Bybit testnet account
- Telegram bot token

## Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/bybit-scalping-bot.git
cd bybit-scalping-bot
```

2. Create and activate a virtual environment:
```bash
python3.12 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Create a `.env` file with your API keys:
```
BYBIT_API_KEY=your_api_key
BYBIT_API_SECRET=your_api_secret
```

## Usage

1. Start the bot:
```bash
python bybit_scalping_bot.py
```

2. Available Telegram commands:
- `/status` - Show current bot status
- `/closeall` - Close all open positions
- `/showchart` - Display candlestick chart
- `/refreshcoins` - Update list of volatile coins

## Configuration

Key parameters in the code:
- `LEVERAGE` - Trading leverage (default: 5)
- `POSITION_SIZE` - Position size in BTC (default: 0.001)
- `STOP_LOSS_PCT` - Stop loss percentage (default: 0.5%)
- `TAKE_PROFIT_PCT` - Take profit percentage (default: 1%)
- `MAX_LOSS_PCT` - Maximum loss percentage (default: 5%)

## Disclaimer

This bot is for educational purposes only. Use at your own risk. Always test thoroughly on testnet before using with real funds.

## License

MIT License
