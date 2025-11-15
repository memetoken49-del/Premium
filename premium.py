#!/usr/bin/env python3
import os
import asyncio
import requests
from telethon import TelegramClient, events
from flask import Flask
import threading
import json
from datetime import datetime
import time

# -----------------------------
# ENVIRONMENT VARIABLES
# -----------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
POSTED_FILE = "posted_coins.json"

# Load posted coins
if os.path.exists(POSTED_FILE):
    with open(POSTED_FILE, "r") as f:
        POSTED_COINS = json.load(f)
else:
    POSTED_COINS = []

# -----------------------------
# TELEGRAM CLIENT
# -----------------------------
client = TelegramClient("pre_pump_session", API_ID, API_HASH)

# -----------------------------
# FLASK KEEP-ALIVE (Render)
# -----------------------------
app = Flask(__name__)
@app.route("/")
def home():
    return "✅ Pre-Top-Gainer Scanner Bot Running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def self_ping():
    while True:
        try:
            url = os.environ.get("RENDER_URL")
            if url:
                requests.get(url)
                print(f"[{datetime.now()}] 🔁 Self-ping to {url}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Self-ping error: {e}")
        time.sleep(240)  # every 4 minutes

# -----------------------------
# HELPER FUNCTIONS
# -----------------------------
def is_stable(symbol):
    return any(s in symbol.upper() for s in ["USDT","BUSD","USDC","DAI","TUSD"])

def calculate_buy_sell_zones(price):
    percentages = [0.05, 0.12, 0.20, 0.35, 0.55, 0.85, 1.00]
    sell_zones = [round(price*(1+x),6) for x in percentages]
    buy_zone_1 = round(price*0.98,6)
    buy_zone_2 = round(price*0.995 * 1.015,6)  # 1.5% allowance
    return buy_zone_1, buy_zone_2, sell_zones

async def fetch_coins(per_page=100, total_pages=2, spacing=2):
    coins = []
    for page in range(1, total_pages+1):
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "sparkline": "false"
        }
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            coins.extend(response.json())
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Fetch error page {page}: {e}")
        await asyncio.sleep(spacing)
    return coins

async def fetch_ohlc(coin_id, days=1):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": days}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()  # [[timestamp, open, high, low, close, volume], ...]
    except Exception as e:
        print(f"[{datetime.now()}] ❌ OHLC fetch error {coin_id}: {e}")
        return []

async def post_signal(c):
    global POSTED_COINS
    symbol = c["symbol"].upper() + "/USDT (Binance)"
    if symbol in POSTED_COINS:
        return
    price = c["current_price"]
    buy1, buy2, sells = calculate_buy_sell_zones(price)
    msg = f"🚀 {symbol}\n"
    msg += f"Buy zone {buy1}-{buy2}\n"
    msg += "Sell zone " + "-".join([str(sz) for sz in sells]) + "\n"
    msg += "Margin 3x"
    await client.send_message(CHANNEL_ID, msg)
    POSTED_COINS.append(symbol)
    if len(POSTED_COINS) > 20:
        POSTED_COINS.pop(0)
    with open(POSTED_FILE, "w") as f:
        json.dump(POSTED_COINS, f)

# -----------------------------
# SCAN AND POST PRE-TOP-GAINER COINS
# -----------------------------
async def scan_and_post():
    coins = await fetch_coins(per_page=250, total_pages=2)
    candidates = []

    for c in coins:
        symbol = c["symbol"].upper()
        if is_stable(symbol):
            continue

        total_volume = c.get("total_volume", 0)
        market_cap = c.get("market_cap", 100_000_000)
        if total_volume < 50_000:
            continue

        ohlc = await fetch_ohlc(c['id'], days=1)
        if len(ohlc) < 5:
            continue

        last_candles = ohlc[-5:]
        opens = [x[1] for x in last_candles]
        highs = [x[2] for x in last_candles]
        lows = [x[3] for x in last_candles]
        closes = [x[4] for x in last_candles]
        volumes = [x[5] for x in last_candles]

        # Micro-move
        last_move_pct = (closes[-1] - closes[-2]) / closes[-2] * 100
        if last_move_pct < 0.5:
            continue

        # Consolidation breakout: last 4 candles range < 2%
        last_range = max(highs[-4:]) - min(lows[-4:])
        if last_range / closes[-1] > 0.02:
            continue

        # Volume acceleration: last candle volume vs avg previous 3
        avg_prev_vol = sum(volumes[:-1]) / len(volumes[:-1])
        vol_accel = min(volumes[-1] / avg_prev_vol, 2.0)

        # Market cap bonus
        cap_bonus = min(max(50_000_000 / market_cap, 0.8), 1.2)

        # Signal score
        score = 0.5*(last_move_pct/5) + 0.3*vol_accel + 0.2*cap_bonus

        if score >= 0.75:
            candidates.append((score, c))

    # Sort top 7 candidates
    candidates.sort(key=lambda x: x[0], reverse=True)
    top_candidates = [c[1] for c in candidates[:7]]

    posted = 0
    for coin in top_candidates:
        await post_signal(coin)
        posted += 1

    # Notify admin only if no coins found
    if posted == 0:
        try:
            await client.send_message(ADMIN_ID, f"❌ No suitable pre-top-gainer candidates found at {datetime.now()}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Failed to notify admin: {e}")

# -----------------------------
# AUTOMATIC SCAN LOOP
# -----------------------------
SCAN_INTERVAL = 600  # 10 minutes

async def auto_scan_loop():
    while True:
        try:
            print(f"[{datetime.now()}] ⏳ Starting automatic scan...")
            await scan_and_post()
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error in auto scan: {e}")
        print(f"[{datetime.now()}] ✅ Scan completed. Waiting {SCAN_INTERVAL} seconds...")
        await asyncio.sleep(SCAN_INTERVAL)

# -----------------------------
# TELEGRAM /signal COMMAND
# -----------------------------
@client.on(events.NewMessage(pattern="/signal"))
async def manual_trigger(event):
    user_id = event.sender_id
    if user_id != ADMIN_ID:
        await event.reply("❌ You are not authorized.")
        return
    await event.reply("⏳ Manual scan started — looking for up to 7 coin(s). This may take a few minutes...")
    await scan_and_post()
    await event.reply("✅ Manual scan completed.")

# -----------------------------
# MAIN
# -----------------------------
async def main():
    await client.start(bot_token=BOT_TOKEN)
    print("✅ Pre-Top-Gainer Scanner Bot is live")
    # Start automatic scan loop
    asyncio.create_task(auto_scan_loop())
    await client.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    asyncio.run(main())
