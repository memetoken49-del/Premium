#!/usr/bin/env python3
# premium_rest_advanced_fixed.py
# REST-only Binance Pre-Pump Scanner (Advanced 4-factor detection, Upstash-safe, TP watcher removed)

import os
import asyncio
import threading
import json
import time
import random
from datetime import datetime, timezone, timedelta
from collections import deque

import requests
from flask import Flask
from telethon import TelegramClient
from binance import AsyncClient

# -----------------------------
# ENVIRONMENT / TUNABLES
# -----------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

UPSTASH_REST_URL = os.getenv("UPSTASH_REST_URL", "")
UPSTASH_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_TOKEN", "")

# Detection params (tweak these)
VOLUME_SPIKE_THRESHOLD = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "2.5"))  # volatility multiplier
PRICE_ACCEL_THRESHOLD = float(os.getenv("PRICE_ACCEL_THRESHOLD", "0.2"))    # percent acceleration threshold
UPTICK_STREAK = int(os.getenv("UPTICK_STREAK", "3"))
MA_SHORT_LEN = int(os.getenv("MA_SHORT_LEN", "5"))
MA_LONG_LEN = int(os.getenv("MA_LONG_LEN", "20"))
TRADE_WINDOW_SIZE = int(os.getenv("TRADE_WINDOW_SIZE", "30"))

MAX_SIGNALS_PER_DAY = int(os.getenv("MAX_SIGNALS_PER_DAY", "10"))
SIGNAL_WINDOW_HOURS = int(os.getenv("SIGNAL_WINDOW_HOURS", "24"))

GROUP_SIZE = int(os.getenv("GROUP_SIZE", "110"))

# Upstash / interval tuning (keeps you under commands)
UPSTASH_MONTH_LIMIT = int(os.getenv("UPSTASH_MONTH_LIMIT", "500000"))
COINS_COUNT_APPROX = int(os.getenv("COINS_COUNT_APPROX", "440"))
DAYS_IN_MONTH = int(os.getenv("DAYS_IN_MONTH", "30"))
max_polls_month = UPSTASH_MONTH_LIMIT // max(1, COINS_COUNT_APPROX)
polls_per_day = max_polls_month / max(1, DAYS_IN_MONTH)
FULL_LOOP_INTERVAL = int(24 * 60 * 60 / max(1, polls_per_day))  # seconds per full loop

# -----------------------------
# TELEGRAM / FLASK / CLIENTS
# -----------------------------
tg_client = TelegramClient("pre_pump_session", API_ID, API_HASH)
tg_semaphore = asyncio.Semaphore(1)

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
            try:
                requests.get(url, timeout=10)
            except Exception:
                pass
        time.sleep(240)

# -----------------------------
# UPSTASH HELPERS (sync -> thread)
# -----------------------------
UP_HEADERS = {"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"}

def upstash_set_sync(key, value):
    try:
        resp = requests.post(f"{UPSTASH_REST_URL}/set/{key}", headers=UP_HEADERS, data=json.dumps(value), timeout=12)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash SET error key={key}: {e}")
        return None

def upstash_get_sync(key):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/get/{key}", headers=UP_HEADERS, timeout=12)
        resp.raise_for_status()
        return resp.json().get("result")
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash GET error key={key}: {e}")
        return None

def upstash_sadd_sync(setname, member):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/sadd/{setname}/{member}", headers=UP_HEADERS, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash SADD error {setname} {member}: {e}")
        return None

def upstash_srem_sync(setname, member):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/srem/{setname}/{member}", headers=UP_HEADERS, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash SREM error {setname} {member}: {e}")
        return None

def upstash_smembers_sync(setname):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/smembers/{setname}", headers=UP_HEADERS, timeout=12)
        resp.raise_for_status()
        return resp.json().get("result") or []
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash SMEMBERS error {setname}: {e}")
        return []

# Async wrappers
async def upstash_set(key, value): return await asyncio.to_thread(upstash_set_sync, key, value)
async def upstash_get(key): return await asyncio.to_thread(upstash_get_sync, key)
async def upstash_sadd(setname, member): return await asyncio.to_thread(upstash_sadd_sync, setname, member)
async def upstash_srem(setname, member): return await asyncio.to_thread(upstash_srem_sync, setname, member)
async def upstash_smembers(setname): return await asyncio.to_thread(upstash_smembers_sync, setname)

# -----------------------------
# STATE
# -----------------------------
# Per-symbol state; will be kept in memory.
symbol_state = {}
# structure per symbol:
# {
#   "trades": deque,
#   "last_avg_price": float,
#   "vol_history": deque,
#   "upticks": int,
#   "ma_short": deque,
#   "ma_long": deque,
#   "prev_price": float,
#   "prev_prev_price": float
# }

# -----------------------------
# UTIL / SIGNALS
# -----------------------------
def calculate_buy_sell_zones(price: float):
    sell_perc = [0.05, 0.12, 0.20, 0.35, 0.55, 0.85, 1.0]
    sell_zones = [round(price*(1+x), 6) for x in sell_perc]
    buy1 = round(price * 0.98, 6)
    buy2 = round(price * 0.995 * 1.015, 6)
    return buy1, buy2, sell_zones

async def safe_send_telegram(msg, reply_to=None):
    async with tg_semaphore:
        try:
            if reply_to:
                sent = await tg_client.send_message(CHANNEL_ID, msg, reply_to=reply_to)
            else:
                sent = await tg_client.send_message(CHANNEL_ID, msg)
            return getattr(sent, "id", None)
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Telegram send failed: {e}")
            return None

async def can_post_signal(symbol):
    signals_today = await upstash_smembers("signals_today") or []
    if len(signals_today) >= MAX_SIGNALS_PER_DAY:
        return False
    last = await upstash_get(f"last_signal:{symbol}")
    if last:
        try:
            dt = datetime.fromisoformat(last)
            if (datetime.now(timezone.utc) - dt).total_seconds() < SIGNAL_WINDOW_HOURS * 3600:
                return False
        except Exception:
            pass
    return True

async def mark_signal_sent(symbol, payload=None):
    now_iso = datetime.now(timezone.utc).isoformat()
    await upstash_set(f"last_signal:{symbol}", now_iso)
    await upstash_sadd("signals_today", symbol)
    if payload:
        await upstash_set(f"signal:{symbol}", payload)
        await upstash_sadd("active_signals", symbol)

async def post_signal(symbol, price):
    if not await can_post_signal(symbol):
        print(f"[{datetime.now()}] ⏱ Skipped {symbol} by TTL/daily limit")
        return
    buy1, buy2, sells = calculate_buy_sell_zones(price)
    msg = f"🚀 Binance\n#{symbol}/USDT\nBuy zone {buy1}-{buy2}\nSell zone {' - '.join([str(s) for s in sells])}\nMargin 3x"
    msg_id = await safe_send_telegram(msg)
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "msg_ids": [{"msg_id": msg_id, "posted_at": now_iso}],
        "symbol": symbol,
        "buy_price": price,
        "sell_targets": sells,
        "posted_at": now_iso,
        "posted_by": "bot"
    }
    await mark_signal_sent(symbol, payload)
    # set last_price once at signal time
    await upstash_set(f"last_price:{symbol}", {"price": price, "updated_at": now_iso})
    print(f"[{datetime.now()}] ✅ Posted signal {symbol} at {price}")

# -----------------------------
# DAILY RESET
# -----------------------------
async def reset_daily_signals_loop():
    while True:
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        seconds = (tomorrow - now).total_seconds()
        print(f"[{datetime.now()}] ⏱ Next reset in {int(seconds)}s")
        await asyncio.sleep(seconds + 1)
        await upstash_set("signals_today", [])
        print(f"[{datetime.now()}] 🔄 Daily signal counter reset")

# -----------------------------
# OPTIMIZED POLL LOOP (5-min interval)
# -----------------------------
async def poll_once_process(ticker_map, symbols_to_check, active_set, last_price_updates):
    eps = 1e-9
    for symbol in symbols_to_check:
        full = f"{symbol}USDT"
        raw = ticker_map.get(full)
        if raw is None:
            continue
        price = float(raw)

        state = symbol_state.get(symbol)
        if state is None:
            state = {
                "trades": deque(maxlen=TRADE_WINDOW_SIZE),
                "last_avg_price": price,
                "vol_history": deque(maxlen=30),
                "upticks": 0,
                "ma_short": deque(maxlen=MA_SHORT_LEN),
                "ma_long": deque(maxlen=MA_LONG_LEN),
                "prev_price": price,
                "prev_prev_price": price
            }
            symbol_state[symbol] = state

        prev_price = state["last_avg_price"]
        # update rolling "trades" price window
        state["trades"].append({"price": price, "qty": 1.0})
        price_now = sum(t["price"] for t in state["trades"]) / len(state["trades"])
        state["last_avg_price"] = price_now

        # volatility spike
        vol = abs(price_now - prev_price)
        state["vol_history"].append(vol)
        avg_vol = sum(state["vol_history"]) / len(state["vol_history"]) if state["vol_history"] else eps
        vol_spike = vol / (avg_vol + eps)

        # uptick streak
        state["upticks"] = state["upticks"] + 1 if price_now > prev_price else 0

        # MA short / long
        state["ma_short"].append(price_now)
        state["ma_long"].append(price_now)
        ma_short = sum(state["ma_short"]) / len(state["ma_short"])
        ma_long = sum(state["ma_long"]) / len(state["ma_long"])

        # acceleration
        delta1 = price_now - state["prev_price"]
        delta0 = state["prev_price"] - state["prev_prev_price"]
        acceleration = delta1 - delta0
        state["prev_prev_price"] = state["prev_price"]
        state["prev_price"] = price_now
        accel_pct = (acceleration / (state["prev_prev_price"] + eps)) * 100.0

        # 4-factor detection
        if (vol_spike >= VOLUME_SPIKE_THRESHOLD and
            accel_pct >= PRICE_ACCEL_THRESHOLD and
            state["upticks"] >= UPTICK_STREAK and
            ma_short > ma_long):
            asyncio.create_task(post_signal(symbol, price_now))

        # collect last_price updates only for active symbols
        if symbol in active_set:
            last_price_updates[symbol] = {"price": price_now, "updated_at": datetime.now(timezone.utc).isoformat()}


async def safe_poll_loop(client, all_pairs):
    groups = [all_pairs[i:i+GROUP_SIZE] for i in range(0, len(all_pairs), GROUP_SIZE)]
    print(f"[{datetime.now()}] ⏱ Starting optimized poll loop ({len(all_pairs)} pairs)")

    while True:
        try:
            tickers = await client.get_all_tickers()
            ticker_map = {t['symbol']: t['price'] for t in tickers}

            active_list = await upstash_smembers("active_signals") or []
            active_set = set(active_list)
            last_price_updates = {}

            for idx, group in enumerate(groups, start=1):
                await poll_once_process(ticker_map, group, active_set, last_price_updates)
                await asyncio.sleep(0.5 + random.random()*1.5)

            # batch write last_price updates
            for sym, payload in last_price_updates.items():
                await upstash_set(f"last_price:{sym}", payload)

        except Exception as e:
            err = str(e)
            print(f"[{datetime.now()}] ❌ Poll loop error: {err}")
            if "Way too much request weight" in err or "IP banned" in err or "429" in err:
                backoff = 60 + random.randint(30,180)
                print(f"[{datetime.now()}] 🔥 Binance rate-limit — backing off {backoff}s")
                await asyncio.sleep(backoff)

        print(f"[{datetime.now()}] ⏱ Full loop complete — sleeping 300s")
        await asyncio.sleep(300)  # 5 minutes interval
# -----------------------------
# MAIN
# -----------------------------
async def main():
    print(f"[{datetime.now()}] 🟢 Starting Telegram client...")
    await tg_client.start(bot_token=BOT_TOKEN)
    print(f"[{datetime.now()}] 🟢 Creating Binance AsyncClient...")
    client = await AsyncClient.create(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

    # fetch real USDT pairs
    try:
        info = await client.get_exchange_info()
        all_pairs_full = [s["symbol"] for s in info.get("symbols", []) if s["symbol"].endswith("USDT")]
        all_base_symbols = [p[:-4] for p in all_pairs_full]
        print(f"[{datetime.now()}] ℹ Found {len(all_base_symbols)} USDT pairs")
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Failed to fetch exchange info: {e}")
        await client.close_connection()
        raise

    # start tasks: poll loop and daily reset only
    asyncio.create_task(safe_poll_loop(client, all_base_symbols))
    asyncio.create_task(reset_daily_signals_loop())

    print(f"[{datetime.now()}] 🟢 Background tasks started. Bot is running.")
    await tg_client.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted, exiting...")
