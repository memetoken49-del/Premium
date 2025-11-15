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
    return "✅ Pre-Pump Scanner Bot Running"

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

def detect_volume_spike(c, multiplier=1):
    total_volume = c.get("total_volume", 0)
    if total_volume <= 0:
        return 0
    price_change = abs(c.get("price_change_percentage_24h", 0))
    spike_factor = price_change * (total_volume / 1_000_000)
    return spike_factor >= multiplier, spike_factor  # return factor for scoring

def market_cap_bonus(c):
    # Smaller market cap → higher score (bonus between 0.8–1.2)
    cap = c.get("market_cap", 100_000_000)
    if cap <= 0:
        cap = 100_000_000
    bonus = min(max(50_000_000 / cap, 0.8), 1.2)
    return bonus

async def fetch_coins(per_page=50, total_pages=3, spacing=2):
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
# SCAN AND POST PRE-PUMP COINS
# -----------------------------
async def scan_and_post():
    coins = await fetch_coins()
    candidates = []

    for c in coins:
        symbol = c["symbol"].upper()
        if is_stable(symbol):
            continue
        price_change = c.get("price_change_percentage_24h", 0)
        if price_change < 0.3 or price_change > 2:  # micro early pump
            continue
        volume = c.get("total_volume",0)
        if volume < 50_000:  # minimum liquidity
            continue
        spike_detected, spike_factor = detect_volume_spike(c)
        if not spike_detected:
            continue
        score = price_change * 0.6 + spike_factor * 0.3
        score *= market_cap_bonus(c)  # smaller cap bonus
        candidates.append((score, c))

    # Sort by signal score
    candidates.sort(key=lambda x: x[0], reverse=True)
    candidates = [c[1] for c in candidates[:7]]  # top 7

    posted = 0
    for coin in candidates:
        await post_signal(coin)
        posted += 1

    if posted == 0:
        await client.send_message(CHANNEL_ID, "❌ No suitable pre-pump candidates found.")

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
    print("✅ Pre-Pump Scanner Bot is live")
    await client.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    asyncio.run(main())
