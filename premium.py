#!/usr/bin/env python3
# premium_rest_safe_ttl.py - REST-only Binance Pre-Pump Scanner (Upstash + TTL)

import os
import asyncio
import threading
import json
import time
from datetime import datetime, timezone
from collections import deque

import requests
from flask import Flask
from telethon import TelegramClient
from binance import AsyncClient

# -----------------------------
# ENVIRONMENT VARIABLES
# -----------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

UPSTASH_REST_URL = os.getenv("UPSTASH_REST_URL", "")
UPSTASH_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_TOKEN", "")

# -----------------------------
# TELEGRAM CLIENT
# -----------------------------
tg_client = TelegramClient("pre_pump_session", API_ID, API_HASH)

# -----------------------------
# FLASK KEEP-ALIVE
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
        url = os.environ.get("RENDER_URL")
        if url:
            try: requests.get(url, timeout=10)
            except: pass
        time.sleep(240)

# -----------------------------
# UPSTASH HELPERS (TTL support)
# -----------------------------
UP_HEADERS = {"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"}

def upstash_set_sync(key, value):
    """Supports TTL if value is dict {'value':..., 'ex':seconds}"""
    try:
        return requests.post(f"{UPSTASH_REST_URL}/set/{key}", headers=UP_HEADERS, data=json.dumps(value), timeout=12).json()
    except: return {}

def upstash_get_sync(key):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/get/{key}", headers=UP_HEADERS, timeout=12).json()
        return resp.get("result")
    except: return None

# Async wrappers
async def upstash_set(key, value): return await asyncio.to_thread(upstash_set_sync, key, value)
async def upstash_get(key): return await asyncio.to_thread(upstash_get_sync, key)

# -----------------------------
# FILTER / STATE
# -----------------------------
MIN_TRADE_USD = float(os.getenv("MIN_TRADE_USD", "500.0"))
VOLUME_SPIKE_THRESHOLD = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "1.5"))
PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", "0.5"))
TRADE_WINDOW_SIZE = int(os.getenv("TRADE_WINDOW_SIZE", "30"))
REDIS_TTL_SECONDS = 86400  # 24h anti-duplicate

symbol_state = {}  # symbol -> {trades deque, last_avg_price, last_volume}

def calculate_buy_sell_zones(price: float):
    sell_perc = [0.05, 0.12, 0.20, 0.35, 0.55, 0.85, 1.00]
    sell_zones = [round(price*(1+x), 6) for x in sell_perc]
    buy_zone_1 = round(price*0.98, 6)
    buy_zone_2 = round(price*0.995 * 1.015, 6)
    return buy_zone_1, buy_zone_2, sell_zones

# -----------------------------
# SIGNAL POSTING (with full logging)
# -----------------------------
async def post_signal(symbol, price):
    if not await can_post_signal(symbol):
        print(f"[{datetime.now()}] ⏱ Signal for {symbol} blocked by TTL")
        return

    buy1, buy2, sells = calculate_buy_sell_zones(price)
    msg = (
        f"🚀 Binance\n"
        f"#{symbol}/USDT\n"
        f"Buy zone {buy1}-{buy2}\n"
        f"Sell zone {' - '.join([str(sz) for sz in sells])}\n"
        f"Margin 3x"
    )

    try:
        await tg_client.send_message(CHANNEL_ID, msg)
        print(f"[{datetime.now()}] ✅ Posted signal {symbol} at {price}")
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Telegram send failed for {symbol}: {e}")

    await mark_signal_sent(symbol)


# -----------------------------
# REST POLLING LOOP (full logging)
# -----------------------------
async def poll_prices_loop(client_ws):
    print(f"[{datetime.now()}] ⏱ Polling loop started (interval={POLL_INTERVAL_SECONDS}s)")
    signal_count = 0

    while True:
        try:
            tickers = await client_ws.get_all_tickers()
            print(f"[{datetime.now()}] ⚡ Retrieved {len(tickers)} tickers from Binance")

            for t in tickers:
                s = t.get("symbol","")
                if not s.endswith("USDT"):
                    continue

                symbol = s.replace("USDT","")
                price = float(t.get("price", 0))
                state = symbol_state.get(symbol)

                if state is None:
                    state = {
                        "trades": deque(maxlen=TRADE_WINDOW_SIZE),
                        "last_avg_price": price,
                        "last_volume": 1.0
                    }
                    symbol_state[symbol] = state

                state["trades"].append({"price": price, "qty": 1.0})
                trades = state["trades"]
                volume_now = sum(x['price']*x['qty'] for x in trades)
                price_now = sum(x['price'] for x in trades)/len(trades) if trades else price
                prev_volume = state.get("last_volume", 1.0)
                prev_price = state.get("last_avg_price", price_now)
                volume_spike = volume_now / (prev_volume + 1e-9)
                price_change = ((price_now - prev_price) / (prev_price + 1e-9)) * 100
                state["last_volume"] = volume_now
                state["last_avg_price"] = price_now

                print(
                    f"[DEBUG {datetime.now()}] {symbol}: price={price_now:.6f}, "
                    f"volume_spike={volume_spike:.2f}, price_change={price_change:.2f}%"
                )

                if volume_spike >= VOLUME_SPIKE_THRESHOLD and price_change >= PRICE_CHANGE_THRESHOLD:
                    await post_signal(symbol, price_now)
                    signal_count += 1

                    # Delay after every 7 signals
                    if signal_count % 7 == 0:
                        print(f"[{datetime.now()}] ⏳ Delay 5s after 7 signals")
                        await asyncio.sleep(5)

        except Exception as e:
            print(f"[{datetime.now()}] ❌ Poll loop error: {e}")

        print(f"[{datetime.now()}] ⏱ Poll loop sleeping for {POLL_INTERVAL_SECONDS}s")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
# -----------------------------
# DYNAMIC POLL INTERVAL (≈40min)
# -----------------------------
COINS_COUNT = 440
UPSTASH_MONTH_LIMIT = 500_000
DAYS_IN_MONTH = 30

max_polls_month = UPSTASH_MONTH_LIMIT // COINS_COUNT
polls_per_day = max_polls_month / DAYS_IN_MONTH
POLL_INTERVAL_SECONDS = int(24*60*60 / polls_per_day)

# -----------------------------
# REST POLLING LOOP
# -----------------------------
async def poll_prices_loop(client_ws):
    print(f"[{datetime.now()}] ⏱ Polling loop started (interval={POLL_INTERVAL_SECONDS}s)")
    signal_count = 0  # Counter for throttling

    while True:
        try:
            tickers = await client_ws.get_all_tickers()
            for t in tickers:
                s = t.get("symbol","")
                if not s.endswith("USDT"): continue
                symbol = s.replace("USDT","")
                price = float(t.get("price",0))
                state = symbol_state.get(symbol)
                if state is None:
                    state = {"trades": deque(maxlen=TRADE_WINDOW_SIZE), "last_avg_price": price, "last_volume":1.0}
                    symbol_state[symbol] = state
                state["trades"].append({"price":price,"qty":1.0})
                trades = state["trades"]
                volume_now = sum(x['price']*x['qty'] for x in trades)
                price_now = sum(x['price'] for x in trades)/len(trades) if trades else price
                prev_volume = state.get("last_volume",1.0)
                prev_price = state.get("last_avg_price",price_now)
                volume_spike = volume_now / (prev_volume+1e-9)
                price_change = ((price_now-prev_price)/(prev_price+1e-9))*100
                state["last_volume"]=volume_now
                state["last_avg_price"]=price_now

                if volume_spike>=VOLUME_SPIKE_THRESHOLD and price_change>=PRICE_CHANGE_THRESHOLD:
                    await post_signal(symbol, price_now)  # await to control timing
                    signal_count += 1

                    # Delay after every 7 signals
                    if signal_count % 7 == 0:
                        await asyncio.sleep(5)

        except Exception as e:
            print(f"[{datetime.now()}] ❌ Poll loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
# -----------------------------
# MAIN
# -----------------------------
async def main():
    await tg_client.start(bot_token=BOT_TOKEN)
    client_ws = await AsyncClient.create(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    asyncio.create_task(poll_prices_loop(client_ws))
    await tg_client.run_until_disconnected()

if __name__=="__main__":
    threading.Thread(target=run_web,daemon=True).start()
    threading.Thread(target=self_ping,daemon=True).start()
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Interrupted, exiting...")
