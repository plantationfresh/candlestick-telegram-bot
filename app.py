import os
import io
import json
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio

from flask import Flask, request
import requests

class InlineKeyboardButton:
    def __init__(self, text, callback_data=None):
        self.text = text
        self.callback_data = callback_data

    def to_dict(self):
        return {"text": self.text, "callback_data": self.callback_data}


# --- Telegram Setup ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)

# --- Fix Kaleido (Plotly image export) ---
pio.kaleido.scope.default_format = "png"

WATCHLIST_FILE = "watchlist.json"

# --- Load watchlist from file or create default ---
def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "r") as f:
            return json.load(f)
    return {
    "Reliance": "RELIANCE.NS",
    "M&M": "M&M.NS",
    "ARE&M": "ARE&M.NS",
    "SMLISUZU": "SMLISUZU.NS",
    "ASHOKLEY": "ASHOKLEY.NS",
    "EICHER":"EICHERMOT.NS"
    }

def save_watchlist(watchlist):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlist, f)

WATCHLIST = load_watchlist()

# --- RSI Calculation ---
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# --- Chart Function ---
def plot_stock_chart(ticker_symbol, days=365, donchian_window=20):
    end_date = datetime.today() + timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    ticker = yf.Ticker(ticker_symbol)
    ohlc = ticker.history(start=start_date, end=end_date, interval="1d").reset_index()
    ohlc["Date"] = pd.to_datetime(ohlc["Date"]).dt.tz_localize(None)
    ohlc = ohlc[ohlc["Date"] >= start_date].copy()
    ohlc["Date_str"] = ohlc["Date"].dt.strftime("%Y-%m-%d")
    ohlc["RSI"] = calculate_rsi(ohlc["Close"])

    if ohlc.empty:
        raise ValueError(f"No data returned for {ticker_symbol}. Try a different symbol or longer period.")

    last_row = ohlc.iloc[-1]
    pp = (last_row["High"] + last_row["Low"] + last_row["Close"]) / 3
    r1 = (2 * pp) - last_row["Low"]
    s1 = (2 * pp) - last_row["High"]
    r2 = pp + (last_row["High"] - last_row["Low"])
    s2 = pp - (last_row["High"] - last_row["Low"])
    print(f"\n📊 {ticker_symbol} Levels: Pivot={pp:.2f}, R1={r1:.2f}, S1={s1:.2f}, R2={r2:.2f}, S2={s2:.2f}\n")

    ohlc["Donchian_Upper"] = ohlc["High"].rolling(window=donchian_window).max()
    ohlc["Donchian_Lower"] = ohlc["Low"].rolling(window=donchian_window).min()
    ohlc["Donchian_Middle"] = (ohlc["Donchian_Upper"] + ohlc["Donchian_Lower"]) / 2

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        row_heights=[0.6,0.2,0.2], vertical_spacing=0.05,
                        subplot_titles=(f"{ticker_symbol} - Candlestick Chart","RSI (14)","Volume"))

    fig.add_trace(go.Candlestick(x=ohlc["Date_str"], open=ohlc["Open"], high=ohlc["High"],
                                 low=ohlc["Low"], close=ohlc["Close"],
                                 increasing_line_color="green", decreasing_line_color="red",
                                 showlegend=False), row=1,col=1)

    fig.add_trace(go.Scatter(x=ohlc["Date_str"], y=ohlc["Donchian_Upper"],
                             line=dict(color="blue",width=1), name="Donchian Upper"), row=1,col=1)
    fig.add_trace(go.Scatter(x=ohlc["Date_str"], y=ohlc["Donchian_Lower"],
                             line=dict(color="blue",width=1), name="Donchian Lower"), row=1,col=1)
    fig.add_trace(go.Scatter(x=ohlc["Date_str"], y=ohlc["Donchian_Middle"],
                             line=dict(color="blue",width=1,dash="dot"), name="Donchian Mid"), row=1,col=1)

    fig.add_trace(go.Scatter(x=ohlc["Date_str"], y=ohlc["RSI"],
                             mode="lines", line=dict(color="blue"), name="RSI (14)"), row=2,col=1)

    fig.add_hline(y=70, line=dict(color="red", dash="dash"), row=2,col=1)
    fig.add_hline(y=30, line=dict(color="green", dash="dash"), row=2,col=1)

    fig.add_trace(go.Bar(x=ohlc["Date_str"], y=ohlc["Volume"],
                         marker_color="purple", opacity=0.5, name="Volume"), row=3,col=1)

    fig.update_layout(
        title=f"{ticker_symbol} - Last {days} Days",
        template="plotly_white",
        width=1400,   # wider
        height=700,   # shorter
        xaxis_rangeslider_visible=False,
        xaxis=dict(type="category")
    )

    ymin = ohlc["Low"].min()*0.98
    ymax = ohlc["High"].max()*1.02
    fig.update_yaxes(range=[ymin,ymax], row=1,col=1)

    rmin = max(0, ohlc["RSI"].min()*0.98)
    rmax = min(100, ohlc["RSI"].max()*1.02)
    fig.update_yaxes(range=[rmin,rmax], row=2,col=1)

    return fig

# --- Send chart helper ---
def send_chart(chat_id, symbol, days=365):
    try:
        fig = plot_stock_chart(symbol, days)
        buf = io.BytesIO()
        fig.write_image(buf, format="png")
        buf.seek(0)
        requests.post(
            f"{TELEGRAM_API}/sendPhoto",
            data={"chat_id": chat_id},
            files={"photo": buf}
        )
        buf.close()
    except Exception as e:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            data={"chat_id": chat_id, "text": f"Error: {e}"}
        )
    return "ok"

# --- Telegram webhook handler ---
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    global WATCHLIST
    data = request.get_json()

    # --- Handle text messages ---
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")

        if text.startswith("/watchlist"):
            keyboard = [
                [InlineKeyboardButton(name, callback_data=symbol).to_dict()]
                for name, symbol in WATCHLIST.items()
            ]
            reply_markup = {"inline_keyboard": keyboard}

            requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": "📊 Select a stock:", "reply_markup": reply_markup}
            )

        elif text.startswith("/chart"):
            parts = text.split()
            symbol = parts[1] if len(parts) > 1 else "AAPL"
            days = int(parts[2]) if len(parts) > 2 else 180
            return send_chart(chat_id, symbol, days)

        elif text.startswith("/addwatch"):
            try:
                _, name, symbol = text.split(maxsplit=2)
                WATCHLIST[name] = symbol
                save_watchlist(WATCHLIST)
                requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": f"✅ Added {name} -> {symbol} to watchlist"})
            except:
                requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": "Usage: /addwatch Name SYMBOL"})

        elif text.startswith("/removewatch"):
            try:
                _, name = text.split(maxsplit=1)
                if name in WATCHLIST:
                    del WATCHLIST[name]
                    save_watchlist(WATCHLIST)
                    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": f"❌ Removed {name} from watchlist"})
                else:
                    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": f"{name} not found in watchlist"})
            except:
                requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": "Usage: /removewatch Name"})

       elif text.startswith("/bulkwatch"):
            try:
                # Remove the command itself, split by newlines
                lines = text.strip().split("\n")[1:]  # everything after /bulkwatch
                added = []
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) == 2:  # expects exactly 2 parts: Name Symbol
                        name, symbol = parts
                        WATCHLIST[name] = symbol
                        added.append(f"{name} -> {symbol}")
                    else:
                        print(f"Skipping invalid line: {line}")
        
                save_watchlist(WATCHLIST)
        
                if added:
                    msg = "✅ Bulk upload successful:\n" + "\n".join(added)
                else:
                    msg = "⚠️ No valid entries found.\nFormat: NAME SYMBOL"
                
                requests.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={"chat_id": chat_id, "text": msg}
                )
        
            except Exception as e:
                requests.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={"chat_id": chat_id, "text": f"Error in bulk upload: {e}"}
                )



    # --- Handle button presses ---
    if "callback_query" in data:
        query = data["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        symbol = query["data"]
        return send_chart(chat_id, symbol, 180)

    return "ok"

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
