#!/usr/bin/env python3
# premium_rest_safe_optimized.py
# REST-only Binance Pre-Pump Scanner (Upstash + TTL, 40-minute interval, Telegram channel support)

import os
import asyncio
import threading
import json
import time
from datetime import datetime, timezone

import requests
from flask import Flask
from telethon import TelegramClient, events
from binance import AsyncClient

# -----------------------------
# ENV VARIABLES
# -----------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")  # Optional: Telegram channel ID

UPSTASH_REST_URL = os.getenv("UPSTASH_REST_URL", "")
UPSTASH_TOKEN = os.getenv("UPSTASH_TOKEN", "")
UP_HEADERS = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}

SCAN_INTERVAL = 2400  # 40 minutes in seconds
REDIS_TTL_SECONDS = 86400  # 24 hours anti-duplicate filter

# -----------------------------
# TELEGRAM CLIENT
# -----------------------------
client = TelegramClient('scanner_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# -----------------------------
# UPSTASH HELPERS
# -----------------------------
def upstash_set_sync(key, value):
    try:
        if isinstance(value, dict) and "value" in value and "ex" in value:
            return requests.post(
                f"{UPSTASH_REST_URL}/set/{key}",
                headers=UP_HEADERS,
                data=json.dumps(value),
                timeout=10
            ).json()
        return requests.post(
            f"{UPSTASH_REST_URL}/set/{key}",
            headers=UP_HEADERS,
            data=json.dumps({"value": value}),
            timeout=10
        ).json()
    except:
        return {}

def upstash_get_sync(key):
    try:
        r = requests.get(
            f"{UPSTASH_REST_URL}/get/{key}",
            headers=UP_HEADERS,
            timeout=10
        ).json()
        return r.get("result", None)
    except:
        return None

def upstash_del_sync(key):
    try:
        return requests.delete(
            f"{UPSTASH_REST_URL}/del/{key}",
            headers=UP_HEADERS,
            timeout=10
        ).json()
    except:
        return {}

# Async wrappers
async def upstash_set(key, value):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, upstash_set_sync, key, value)

async def upstash_get(key):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, upstash_get_sync, key)

async def upstash_del(key):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, upstash_del_sync, key)

# -----------------------------
# ANTI-DUPLICATE SIGNAL (24 HOURS)
# -----------------------------
async def can_post_signal(symbol):
    key = f"last_signal:{symbol}"
    last = await upstash_get(key)

    if not last:
        return True

    try:
        if isinstance(last, dict) and "value" in last:
            last = last["value"]

        last_dt = datetime.fromisoformat(last)
        if (datetime.now(timezone.utc) - last_dt).total_seconds() < REDIS_TTL_SECONDS:
            return False
    except:
        pass

    return True

async def mark_signal_sent(symbol):
    now_iso = datetime.now(timezone.utc).isoformat()
    await upstash_set(f"last_signal:{symbol}", {"value": now_iso, "ex": REDIS_TTL_SECONDS})

# -----------------------------
# BINANCE PRE-PUMP DETECTION
# -----------------------------
async def detect_signal(symbol, price):
    key = f"price:{symbol}"
    last_price = await upstash_get(key)

    if not last_price:
        await upstash_set(key, price)
        return None

    try:
        last_price = float(last_price)
    except:
        await upstash_set(key, price)
        return None

    change = 0 if last_price == 0 else ((price - last_price) / last_price) * 100
    await upstash_set(key, price)

    if change >= 2.0:
        return change
    return None

# -----------------------------
# SEND SIGNAL
# -----------------------------
async def send_signal(symbol, change, price):
    msg = (
        f"🚨 *PRE-PUMP DETECTED*\n\n"
        f"*Symbol:* `{symbol}`\n"
        f"*Price:* `{price}`\n"
        f"*Change:* `{change:.2f}%`\n"
        f"*Time:* `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
    )
    
    target = CHANNEL_ID or ADMIN_ID
    await client.send_message(int(target), msg, parse_mode="markdown")

# -----------------------------
# MAIN SCANNING LOOP
# -----------------------------
async def scan_loop():
    binance = await AsyncClient.create()

    while True:
        try:
            tickers = await binance.get_ticker_price()

            for t in tickers:
                symbol = t["symbol"]
                if not symbol.endswith("USDT"):
                    continue

                price = float(t["price"])

                change = await detect_signal(symbol, price)
                if change is None:
                    continue

                if not await can_post_signal(symbol):
                    continue

                await send_signal(symbol, change, price)
                await mark_signal_sent(symbol)

            await asyncio.sleep(SCAN_INTERVAL)

        except Exception as e:
            print("Scan error:", e)
            await asyncio.sleep(5)

# -----------------------------
# MANUAL TELEGRAM COMMANDS
# -----------------------------
@client.on(events.NewMessage(pattern="/price (.*)"))
async def manual_price(event):
    symbol = event.pattern_match.group(1).upper()

    try:
        binance = await AsyncClient.create()
        data = await binance.get_symbol_ticker(symbol=symbol)
        price = data["price"]
        await event.respond(f"{symbol} price: {price}")
    except:
        await event.respond("Invalid symbol or Binance error.")

# -----------------------------
# FLASK SELF-PING SERVER
# -----------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot running"

def run_flask():
    app.run(host="0.0.0.0", port=10000)

def self_ping():
    while True:
        try:
            requests.get("http://localhost:10000")
        except:
            pass
        time.sleep(60)

# -----------------------------
# START EVERYTHING
# -----------------------------
def main():
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()

    loop = asyncio.get_event_loop()
    loop.create_task(scan_loop())
    client.run_until_disconnected()

if __name__ == "__main__":
    main()
