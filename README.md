# 📊 Candlestick Telegram Bot

This bot generates candlestick charts with RSI, Donchian channels, and volume using Yahoo Finance data.

## 🚀 Deploy to Railway

1. Fork or clone this repo.
2. Go to [Railway](https://railway.app/), create a project, and select this repo.
3. Add environment variable:
BOT_TOKEN = your_telegram_bot_token
4. Deploy → Railway gives you a URL like `https://mybot.up.railway.app`.
5. Set Telegram webhook:
6. https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://mybot.up.railway.app/<YOUR_BOT_TOKEN>


## ✅ Usage
Send commands in Telegram:

/chart AAPL 180
/chart TSLA
/chart BTC-USD 30
