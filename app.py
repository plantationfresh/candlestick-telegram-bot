import os
import io
import json
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
import threading
import time

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
        json.dump(dict(sorted(watchlist.items())), f, indent=2)

WATCHLIST = load_watchlist()

def send_all_charts(chat_id, days=180):
    """Generate and send charts for every watchlist item, sequentially."""
    items = list(sorted(WATCHLIST.items()))  # [(name, symbol), ...]
    total = len(items)
    if total == 0:
        requests.post(f"{TELEGRAM_API}/sendMessage",
                      json={"chat_id": chat_id, "text": "Watchlist is empty."})
        return

    # announce start
    requests.post(f"{TELEGRAM_API}/sendMessage",
                  json={"chat_id": chat_id, "text": f"📈 Starting {total} charts (last {days} days)..."})

    sent = 0
    for name, symbol in items:
        try:
            fig = plot_stock_chart(symbol, days)
            buf = io.BytesIO()
            # if you set width/height in update_layout, no need to pass here
            fig.write_image(buf, format="png")
            buf.seek(0)

            caption = f"{name} ({symbol}) • {days}d"
            requests.post(f"{TELEGRAM_API}/sendPhoto",
                          data={"chat_id": chat_id, "caption": caption},
                          files={"photo": buf})
            buf.close()
            sent += 1

            # polite pacing to avoid Telegram/YF rate limits
            time.sleep(0.8)
        except Exception as e:
            requests.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": f"⚠️ {name} ({symbol}): {e}"})

    # done
    requests.post(f"{TELEGRAM_API}/sendMessage",
                  json={"chat_id": chat_id, "text": f"✅ Done. Sent {sent}/{total} charts."})

def send_chart_pdf(chat_id, days=180):
    """
    Generate charts for the entire watchlist and send a single PDF.
    Runs in a background thread (caller should spawn it).
    """
    items = list(sorted(WATCHLIST.items()))  # [(name, symbol), ...]
    if not items:
        requests.post(f"{TELEGRAM_API}/sendMessage",
                      json={"chat_id": chat_id, "text": "Watchlist is empty."})
        return

    # Let user know we started
    requests.post(f"{TELEGRAM_API}/sendMessage",
                  json={"chat_id": chat_id, "text": f"📄 Building PDF for {len(items)} charts (last {days} days)..."} )

    # Prepare a PDF in memory
    pdf_buf = io.BytesIO()
    page_size = landscape(A4)  # (width, height)
    c = canvas.Canvas(pdf_buf, pagesize=page_size)
    pw, ph = page_size

    count = 0
    for name, symbol in items:
        try:
            fig = plot_stock_chart(symbol, days)
            img_buf = io.BytesIO()
            # plotly->PNG in memory
            fig.write_image(img_buf, format="png")
            img_buf.seek(0)

            # Fit image onto page preserving aspect ratio with margins
            margin = 24
            max_w = pw - 2*margin
            max_h = ph - 2*margin

            image = ImageReader(img_buf)
            iw, ih = image.getSize()
            scale = min(max_w/iw, max_h/ih)
            draw_w, draw_h = iw*scale, ih*scale
            x = (pw - draw_w) / 2
            y = (ph - draw_h) / 2

            # Optional header text (small)
            c.setFont("Helvetica-Bold", 12)
            c.drawString(margin, ph - margin + 4, f"{name} ({symbol}) • {days}d")

            # Draw image
            c.drawImage(image, x, y, width=draw_w, height=draw_h)

            c.showPage()
            img_buf.close()
            count += 1

            # Gentle pacing in case of large lists
            time.sleep(0.2)

        except Exception as e:
            # Add a page with the error, but keep going
            c.setFont("Helvetica", 12)
            c.drawString(40, ph - 60, f"{name} ({symbol})")
            c.setFont("Helvetica-Oblique", 10)
            c.drawString(40, ph - 80, f"Error: {e}")
            c.showPage()

    c.save()
    pdf_buf.seek(0)

    # Send the PDF
    files = {
        "document": ("watchlist_charts.pdf", pdf_buf, "application/pdf")
    }
    requests.post(f"{TELEGRAM_API}/sendDocument",
                  data={"chat_id": chat_id, "caption": f"✅ {count}/{len(items)} charts • {days}d"},
                  files=files)
    pdf_buf.close()


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
    # --- Fetch OHLC Data with extra buffer so SMA200 is available ---
    buffer_days = 260  # ~1 trading year cushion
    internal_days = max(days + buffer_days, 420)

    end_date = datetime.today() + timedelta(days=1)
    start_date = end_date - timedelta(days=internal_days)

    ticker = yf.Ticker(ticker_symbol)
    ohlc = ticker.history(start=start_date, end=end_date, interval="1d").reset_index()
    ohlc["Date"] = pd.to_datetime(ohlc["Date"]).dt.tz_localize(None)

    # Keep only valid rows
    ohlc = ohlc[ohlc["Date"] >= start_date].copy()

    # Indicators (compute on full internal window)
    ohlc["RSI"] = calculate_rsi(ohlc["Close"])

    # Moving Averages
    ohlc["SMA20"]  = ohlc["Close"].rolling(window=20).mean()
    ohlc["SMA50"]  = ohlc["Close"].rolling(window=50).mean()
    ohlc["SMA200"] = ohlc["Close"].rolling(window=200).mean()
    # Last-resort fallback so something draws even if data is very short
    if ohlc["SMA200"].notna().sum() == 0:
        ohlc["SMA200"] = ohlc["Close"].rolling(window=200, min_periods=1).mean()

    # Donchian Channels
    ohlc["Donchian_Upper"]  = ohlc["High"].rolling(window=donchian_window).max()
    ohlc["Donchian_Lower"]  = ohlc["Low"].rolling(window=donchian_window).min()
    ohlc["Donchian_Middle"] = (ohlc["Donchian_Upper"] + ohlc["Donchian_Lower"]) / 2

    # Slice to last `days` **calendar** days for DISPLAY
    view_start = end_date - timedelta(days=days)
    ohlc_view = ohlc[ohlc["Date"] >= view_start].copy()
    if ohlc_view.empty:
        raise ValueError(f"No data returned for {ticker_symbol} in the last {days} days.")
    ohlc_view["Date_str"] = ohlc_view["Date"].dt.strftime("%Y-%m-%d")

    # Latest levels (use the VIEW window)
    last_row = ohlc_view.iloc[-1]
    pp = (last_row["High"] + last_row["Low"] + last_row["Close"]) / 3
    r1 = (2 * pp) - last_row["Low"]
    s1 = (2 * pp) - last_row["High"]
    r2 = pp + (last_row["High"] - last_row["Low"])
    s2 = pp - (last_row["High"] - last_row["Low"])
    print(f"\n📊 {ticker_symbol} Levels: Pivot={pp:.2f}, R1={r1:.2f}, S1={s1:.2f}, R2={r2:.2f}, S2={s2:.2f}\n")

    # --- Create Subplots (4 rows: Price, RSI, Volume, MAs) ---
    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True,
        row_heights=[0.52, 0.18, 0.12, 0.18],
        vertical_spacing=0.05,
        subplot_titles=(
            f"{ticker_symbol} - Candlestick Chart",
            "RSI (14)",
            "Volume",
            "Moving Averages (20 / 50 / 200)"
        )
    )

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=ohlc_view["Date_str"],
        open=ohlc_view["Open"],
        high=ohlc_view["High"],
        low=ohlc_view["Low"],
        close=ohlc_view["Close"],
        increasing_line_color="green",
        decreasing_line_color="red",
        showlegend=False
    ), row=1, col=1)

    # Donchian Bands
    fig.add_trace(go.Scatter(
        x=ohlc_view["Date_str"], y=ohlc_view["Donchian_Upper"],
        line=dict(color="blue", width=1),
        name="Donchian Upper",
        mode="lines"
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=ohlc_view["Date_str"], y=ohlc_view["Donchian_Lower"],
        line=dict(color="blue", width=1),
        name="Donchian Lower",
        mode="lines"
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=ohlc_view["Date_str"], y=ohlc_view["Donchian_Middle"],
        line=dict(color="blue", width=1, dash="dot"),
        name="Donchian Mid",
        mode="lines"
    ), row=1, col=1)

    # RSI
    fig.add_trace(go.Scatter(
        x=ohlc_view["Date_str"], y=ohlc_view["RSI"],
        mode="lines", line=dict(color="blue"),
        name="RSI (14)"
    ), row=2, col=1)
    fig.add_hline(y=70, line=dict(color="red", dash="dash"), row=2, col=1)
    fig.add_hline(y=30, line=dict(color="green", dash="dash"), row=2, col=1)

    # Volume
    fig.add_trace(go.Bar(
        x=ohlc_view["Date_str"],
        y=ohlc_view["Volume"],
        marker_color="purple",
        name="Volume",
        opacity=0.5
    ), row=3, col=1)

    # Moving Averages (separate pane)
    fig.add_trace(go.Scatter(
        x=ohlc_view["Date_str"], y=ohlc_view["SMA20"],
        mode="lines", line=dict(width=2, color="orange"),
        name="SMA 20"
    ), row=4, col=1)
    fig.add_trace(go.Scatter(
        x=ohlc_view["Date_str"], y=ohlc_view["SMA50"],
        mode="lines", line=dict(width=2, color="purple"),
        name="SMA 50"
    ), row=4, col=1)
    # plot only valid SMA200 points (avoids drawing nothing if early NaNs exist)
    valid200 = ohlc_view["SMA200"].notna()
    fig.add_trace(go.Scatter(
        x=ohlc_view.loc[valid200, "Date_str"],
        y=ohlc_view.loc[valid200, "SMA200"],
        mode="lines", line=dict(width=2, color="gray"),
        name="SMA 200"
    ), row=4, col=1)

    # Pivot & S/R lines (price pane)
    fig.add_hline(y=pp, line=dict(color="black", width=1, dash="dot"),
                  annotation_text="", annotation_position="top left",
                  row=1, col=1)
    fig.add_hline(y=r1, line=dict(color="red", width=1, dash="dash"),
                  annotation_text="", annotation_position="top left",
                  row=1, col=1)
    fig.add_hline(y=r2, line=dict(color="red", width=1, dash="dashdot"),
                  annotation_text="", annotation_position="top left",
                  row=1, col=1)
    fig.add_hline(y=s1, line=dict(color="green", width=1, dash="dash"),
                  annotation_text="", annotation_position="bottom left",
                  row=1, col=1)
    fig.add_hline(y=s2, line=dict(color="green", width=1, dash="dashdot"),
                  annotation_text="", annotation_position="bottom left",
                  row=1, col=1)

    # Levels info box
    levels_text = (
        f"<b>{ticker_symbol} Levels</b><br>"
        f"PP: {pp:.2f}<br>"
        f"R1: {r1:.2f} &nbsp; R2: {r2:.2f}<br>"
        f"S1: {s1:.2f} &nbsp; S2: {s2:.2f}"
    )
    fig.add_annotation(
        xref="paper", yref="paper",
        x=0.01, y=0.98,
        showarrow=False,
        align="left",
        bordercolor="rgba(0,0,0,0.15)",
        borderwidth=1,
        bgcolor="rgba(255,255,255,0.7)",
        text=levels_text
    )

    # Layout (landscape, Mac screen friendly)
    fig.update_layout(
        title=f"{ticker_symbol} - Last {days} Days",
        template="plotly_white",
        width=1600,    # nice wide aspect for Mac
        height=800,    # shorter, fits screen better
        xaxis_rangeslider_visible=False,
        xaxis=dict(type="category")
    )

    # Axis ranges based on the VIEW window
    ymin = ohlc_view["Low"].min() * 0.98
    ymax = ohlc_view["High"].max() * 1.02
    fig.update_yaxes(range=[ymin, ymax], row=1, col=1)

    rmin = max(0, ohlc_view["RSI"].min() * 0.98)
    rmax = min(100, ohlc_view["RSI"].max() * 1.02)
    fig.update_yaxes(range=[rmin, rmax], row=2, col=1)

    # Optional: tidy MA pane range
    ma_min = pd.concat(
        [ohlc_view["SMA20"], ohlc_view["SMA50"], ohlc_view["SMA200"]], axis=1
    ).min().min()
    
    ma_max = pd.concat(
        [ohlc_view["SMA20"], ohlc_view["SMA50"], ohlc_view["SMA200"]], axis=1
    ).max().max()
    
    if pd.notna(ma_min) and pd.notna(ma_max):
        fig.update_yaxes(range=[ma_min * 0.98, ma_max * 1.02], row=4, col=1)

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

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    """
    Handles incoming Telegram updates.
    Fixes duplicate-photo bug by:
      1. Handling callback_query first and returning immediately.
      2. Answering the callback query (avoids client-side loading spinner).
      3. Only then handling plain messages (commands).
    """
    global WATCHLIST
    data = request.get_json()

    # 1) Handle inline button presses (callback_query) FIRST and return immediately.
    if "callback_query" in data:
        try:
            query = data["callback_query"]
            callback_id = query.get("id")
            # Some callback queries include the message dict; get chat id reliably:
            chat_id = query["message"]["chat"]["id"]
            symbol = query.get("data")

            # Answer the callback so the Telegram client stops the "loading" state.
            # This is optional but recommended.
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={"callback_query_id": callback_id}
            )

            # Send the chart (default days = 180)
            return send_chart(chat_id, symbol, 180)
        except Exception as e:
            # If anything goes wrong here, tell the user and return
            try:
                requests.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={"chat_id": chat_id, "text": f"Error handling button press: {e}"}
                )
            except:
                pass
            return "ok"

    # 2) Then handle normal message updates (commands)
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")

        # /watchlist - show inline keyboard (sorted)
        if text.startswith("/watchlist"):
            keyboard = [
                [InlineKeyboardButton(name, callback_data=symbol).to_dict()]
                for name, symbol in sorted(WATCHLIST.items())
            ]
            reply_markup = {"inline_keyboard": keyboard}
            requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": "📊 Select a stock:", "reply_markup": reply_markup}
            )
            return "ok"

        elif text.split()[0] == "/chartpdf":
            try:
                parts = text.split()
                days = int(parts[1]) if len(parts) > 1 else 180
            except:
                days = 180
        
            # Run in background so webhook returns immediately
            threading.Thread(target=send_chart_pdf, args=(chat_id, days), daemon=True).start()
        
            # Quick ack to avoid Telegram retries
            requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": f"🕒 Generating PDF for all watchlist charts (last {days} days)..."}
            )
            return "ok"

        # /chart SYMBOL [days]
        elif text.split()[0] == "/chart":
            parts = text.split()
            symbol = parts[1] if len(parts) > 1 else "AAPL"
            try:
                days = int(parts[2]) if len(parts) > 2 else 180
            except:
                days = 180
            return send_chart(chat_id, symbol, days)

        # /addwatch Name SYMBOL
        elif text.startswith("/addwatch"):
            try:
                # allow names with spaces; last token is symbol
                parts = text.split()
                if len(parts) < 3:
                    raise ValueError("Usage: /addwatch Name SYMBOL")
                name = "_".join(parts[1:-1])
                symbol = parts[-1]
                WATCHLIST[name] = symbol
                save_watchlist(WATCHLIST)
                requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": f"✅ Added {name} -> {symbol} to watchlist"})
            except Exception as e:
                requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": f"Usage: /addwatch Name SYMBOL\nError: {e}"})
            return "ok"

        # /removewatch Name
        elif text.startswith("/removewatch"):
            try:
                _, name = text.split(maxsplit=1)
                name = name.replace(" ", "_")
                if name in WATCHLIST:
                    del WATCHLIST[name]
                    save_watchlist(WATCHLIST)
                    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": f"❌ Removed {name} from watchlist"})
                else:
                    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": f"{name} not found in watchlist"})
            except:
                requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": "Usage: /removewatch Name"})
            return "ok"

        # /bulkwatch multi-line
        elif text.startswith("/bulkwatch"):
            try:
                lines = text.strip().split("\n")[1:]  # everything after /bulkwatch
                added = []
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        # name can be multiple words -> join them with underscores
                        name = "_".join(parts[:-1])
                        symbol = parts[-1]
                        WATCHLIST[name] = symbol
                        added.append(f"{name} -> {symbol}")
                    else:
                        print(f"Skipping invalid line: {line}")

                save_watchlist(WATCHLIST)

                if added:
                    msg = "✅ Bulk upload successful:\n" + "\n".join(added)
                else:
                    msg = "⚠️ No valid entries found.\nFormat: NAME SYMBOL"
                requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": msg})
            except Exception as e:
                requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": f"Error in bulk upload: {e}"})
            return "ok"

        # /mywatchlist - show plain text list (sorted)
        elif text.startswith("/mywatchlist"):
            try:
                items = [f"{name} -> {symbol}" for name, symbol in sorted(WATCHLIST.items())]
                text_out = "📋 Watchlist:\n" + "\n".join(items) if items else "Watchlist is empty."
                requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text_out})
            except Exception as e:
                requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": f"Error: {e}"})
            return "ok"

        elif text.startswith("/chartall"):
            try:
                parts = text.split()
                days = int(parts[1]) if len(parts) > 1 else 180
            except:
                days = 180
        
            # run in background so webhook returns fast
            threading.Thread(target=send_all_charts, args=(chat_id, days), daemon=True).start()
        
            # quick ack so Telegram doesn’t retry
            requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": f"🕒 Generating charts for all watchlist symbols (last {days} days)..."}
            )
            return "ok"

    # Default return
    return "ok"


@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
