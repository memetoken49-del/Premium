#!/usr/bin/env python3
# premium_rest_safe_rest_opt.py - REST-only Binance Pre-Pump Scanner (Weight-safe, Upstash, TP watcher)

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

# Safety defaults
MIN_TRADE_USD = float(os.getenv("MIN_TRADE_USD", "500.0"))
VOLUME_SPIKE_THRESHOLD = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "1.5"))
PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", "0.5"))
TRADE_WINDOW_SIZE = int(os.getenv("TRADE_WINDOW_SIZE", "30"))

MAX_SIGNALS_PER_DAY = int(os.getenv("MAX_SIGNALS_PER_DAY", "10"))
SIGNAL_WINDOW_HOURS = int(os.getenv("SIGNAL_WINDOW_HOURS", "24"))

# Grouping to avoid weight spikes
GROUP_SIZE = int(os.getenv("GROUP_SIZE", "110"))  # tune if needed

# Upstash / rate limits
UPSTASH_MONTH_LIMIT = int(os.getenv("UPSTASH_MONTH_LIMIT", "500000"))
COINS_COUNT_APPROX = int(os.getenv("COINS_COUNT_APPROX", "440"))
DAYS_IN_MONTH = int(os.getenv("DAYS_IN_MONTH", "30"))
max_polls_month = UPSTASH_MONTH_LIMIT // max(1, COINS_COUNT_APPROX)
polls_per_day = max_polls_month / max(1, DAYS_IN_MONTH)
FULL_LOOP_INTERVAL = int(24*60*60 / polls_per_day)  # seconds per full loop, ~38min with defaults

# -----------------------------
# TELEGRAM CLIENT + semaphore
# -----------------------------
tg_client = TelegramClient("pre_pump_session", API_ID, API_HASH)
tg_semaphore = asyncio.Semaphore(1)  # control Telegram concurrent sends

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
            try:
                requests.get(url, timeout=10)
            except Exception as e:
                print(f"[{datetime.now()}] ⚠ self_ping failed: {e}")
        time.sleep(240)

# -----------------------------
# UPSTASH HELPERS (with logging)
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
        print(f"[{datetime.now()}] ❌ Upstash SADD error set={setname} member={member}: {e}")
        return None

def upstash_srem_sync(setname, member):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/srem/{setname}/{member}", headers=UP_HEADERS, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash SREM error set={setname} member={member}: {e}")
        return None

def upstash_smembers_sync(setname):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/smembers/{setname}", headers=UP_HEADERS, timeout=12)
        resp.raise_for_status()
        return resp.json().get("result") or []
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash SMEMBERS error set={setname}: {e}")
        return []

# Async wrappers
async def upstash_set(key, value): return await asyncio.to_thread(upstash_set_sync, key, value)
async def upstash_get(key): return await asyncio.to_thread(upstash_get_sync, key)
async def upstash_sadd(setname, member): return await asyncio.to_thread(upstash_sadd_sync, setname, member)
async def upstash_srem(setname, member): return await asyncio.to_thread(upstash_srem_sync, setname, member)
async def upstash_smembers(setname): return await asyncio.to_thread(upstash_smembers_sync, setname)

# -----------------------------
# STATE / HELPERS
# -----------------------------
symbol_state = {}  # symbol -> {"trades": deque, "last_avg_price": float, "last_volume": float}

def calculate_buy_sell_zones(price: float):
    sell_perc = [0.05, 0.12, 0.20, 0.35, 0.55, 0.85, 1.00]
    sell_zones = [round(price*(1+x), 6) for x in sell_perc]
    buy_zone_1 = round(price*0.98, 6)
    buy_zone_2 = round(price*0.995 * 1.015, 6)
    return buy_zone_1, buy_zone_2, sell_zones

async def can_post_signal(symbol):
    signals_today = await upstash_smembers("signals_today") or []
    if len(signals_today) >= MAX_SIGNALS_PER_DAY:
        return False
    key = f"last_signal:{symbol}"
    last = await upstash_get(key)
    if last:
        try:
            dt = datetime.fromisoformat(last)
            if (datetime.now(timezone.utc) - dt).total_seconds() < SIGNAL_WINDOW_HOURS * 3600:
                return False
        except Exception:
            pass
    return True

async def mark_signal_sent(symbol, extra_payload=None):
    now_iso = datetime.now(timezone.utc).isoformat()
    await upstash_set(f"last_signal:{symbol}", now_iso)
    await upstash_sadd("signals_today", symbol)
    if extra_payload:
        await upstash_set(f"signal:{symbol}", extra_payload)
        await upstash_sadd("active_signals", symbol)

# Telegram send wrapper with semaphore and logging
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

# -----------------------------
# SIGNAL POSTING
# -----------------------------
async def post_signal(symbol_short, price):
    symbol = symbol_short.upper()
    if not await can_post_signal(symbol):
        print(f"[{datetime.now()}] ⏱ Skipped {symbol} by TTL or daily limit")
        return

    buy1, buy2, sells = calculate_buy_sell_zones(price)
    msg = (
        f"🚀 Binance\n"
        f"#{symbol}/USDT\n"
        f"Buy zone {buy1}-{buy2}\n"
        f"Sell zone {' - '.join([str(sz) for sz in sells])}\n"
        f"Margin 3x"
    )

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

    # store payload and mark active (only when a signal is actually posted)
    await mark_signal_sent(symbol, payload)
    # Also store last_price for active monitoring
    await upstash_set(f"last_price:{symbol}", {"price": price, "updated_at": now_iso})
    print(f"[{datetime.now()}] ✅ Posted signal {symbol} at {price}")

# -----------------------------
# RESET DAILY SIGNALS
# -----------------------------
async def reset_daily_signals_loop():
    while True:
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_midnight = (tomorrow - now).total_seconds()
        print(f"[{datetime.now()}] ⏱ Next daily reset in {int(seconds_until_midnight)}s")
        await asyncio.sleep(seconds_until_midnight + 1)
        await upstash_set("signals_today", [])
        print(f"[{datetime.now()}] 🔄 Daily signal counter reset")

# -----------------------------
# POLLING (weight-safe)
# -----------------------------
async def poll_once_process(ticker_map, symbols_to_check):
    """Process a set of symbols using the ticker_map (symbol->price). Writes last_price only when needed."""
    for symbol in symbols_to_check:
        full = f"{symbol}USDT"
        info_price = ticker_map.get(full)
        if info_price is None:
            continue
        price = float(info_price)
        state = symbol_state.get(symbol)
        if state is None:
            state = {"trades": deque(maxlen=TRADE_WINDOW_SIZE), "last_avg_price": price, "last_volume": 1.0}
            symbol_state[symbol] = state

        # Append fake trade (we don't have real trade qty) — you can replace this with real trade qty if available
        state["trades"].append({"price": price, "qty": 1.0})
        trades = state["trades"]
        volume_now = sum(x['price'] * x['qty'] for x in trades)
        price_now = sum(x['price'] for x in trades) / len(trades) if trades else price
        prev_volume = state.get("last_volume", 1.0)
        prev_price = state.get("last_avg_price", price_now)
        volume_spike = volume_now / (prev_volume + 1e-9)
        price_change = ((price_now - prev_price) / (prev_price + 1e-9)) * 100
        state["last_volume"] = volume_now
        state["last_avg_price"] = price_now

        # If conditions met, post signal
        if volume_spike >= VOLUME_SPIKE_THRESHOLD and price_change >= PRICE_CHANGE_THRESHOLD:
            # post and mark last_price inside post_signal
            asyncio.create_task(post_signal(symbol, price_now))

        # Only set last_price for active signals to reduce Upstash writes
        active = await upstash_smembers("active_signals") or []
        if symbol in active:
            await upstash_set(f"last_price:{symbol}", {"price": price_now, "updated_at": datetime.now(timezone.utc).isoformat()})

async def safe_poll_loop(client, all_pairs):
    # split all_pairs into groups
    groups = [all_pairs[i:i+GROUP_SIZE] for i in range(0, len(all_pairs), GROUP_SIZE)]
    print(f"[{datetime.now()}] ⏱ Poll loop starting: {len(all_pairs)} pairs, {len(groups)} groups, full loop interval {FULL_LOOP_INTERVAL}s")

    while True:
        try:
            # Single heavy call per full loop: get_all_tickers once
            tickers = await client.get_all_tickers()
            # create a fast lookup map
            ticker_map = {t['symbol']: t['price'] for t in tickers}
            print(f"[{datetime.now()}] ⚡ Retrieved {len(ticker_map)} tickers")

            # process groups sequentially with a short delay to spread CPU/upstash load (not Binance weight)
            for idx, group in enumerate(groups, start=1):
                print(f"[{datetime.now()}] ▶ Processing group {idx}/{len(groups)} (size {len(group)})")
                await poll_once_process(ticker_map, group)
                # small delay to avoid Upstash bursts or other concurrent loads
                await asyncio.sleep(1 + random.random()*2)

        except Exception as e:
            err = str(e)
            print(f"[{datetime.now()}] ❌ Poll loop error: {err}")
            # detect Binance rate-limit or IP ban messages and back off longer
            if "Way too much request weight" in err or "IP banned" in err or "429" in err:
                backoff = 60 + random.randint(30, 180)
                print(f"[{datetime.now()}] 🔥 Detected Binance rate-limit — backing off for {backoff}s")
                await asyncio.sleep(backoff)
        # Wait until next full loop
        print(f"[{datetime.now()}] ⏱ Full loop complete — sleeping {FULL_LOOP_INTERVAL}s")
        await asyncio.sleep(FULL_LOOP_INTERVAL)

# -----------------------------
# TP WATCHER
# -----------------------------
async def tp_watcher_loop():
    print(f"[{datetime.now()}] ⏱ TP watcher started")
    while True:
        try:
            symbols = await upstash_smembers("active_signals") or []
            if not symbols:
                await asyncio.sleep(60)
                continue
            for symbol in symbols:
                key = f"signal:{symbol}"
                data = await upstash_get(key)
                if not data:
                    await upstash_srem("active_signals", symbol)
                    continue
                # normalize data
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except Exception:
                        data = {}
                lp = await upstash_get(f"last_price:{symbol}")
                if not lp or not isinstance(lp, dict):
                    continue
                try:
                    current_price = float(lp.get("price", 0))
                except Exception:
                    continue
                buy_price = float(data.get("buy_price", 0))
                sell_targets = data.get("sell_targets", []) or []
                hit_index = None
                for idx, t in enumerate(sell_targets):
                    try:
                        if current_price >= float(t):
                            hit_index = idx
                            break
                    except Exception:
                        continue
                if hit_index is not None:
                    profit_pct = ((float(sell_targets[hit_index]) - buy_price) / buy_price) * 100 * 3.0
                    msg = f"#{symbol}/USDT TP {hit_index+1} ✅\nProfit: {profit_pct:.4f}% 📈"
                    try:
                        msgs = data.get("msg_ids", [])
                        orig_msg_id = msgs[-1].get("msg_id") if msgs else None
                        if orig_msg_id:
                            await safe_send_telegram(msg, reply_to=orig_msg_id)
                        else:
                            await safe_send_telegram(msg)
                    except Exception as e:
                        print(f"[{datetime.now()}] ❌ TP notify failed for {symbol}: {e}")

                    # update sell targets
                    new_targets = sell_targets[hit_index+1:]
                    if new_targets:
                        data["sell_targets"] = new_targets
                        await upstash_set(key, data)
                    else:
                        await upstash_srem("active_signals", symbol)
                        await upstash_set(key, None)
        except Exception as e:
            print(f"[{datetime.now()}] ❌ TP watcher error: {e}")
        await asyncio.sleep(60)

# -----------------------------
# STARTUP / MAIN
# -----------------------------
async def main():
    print(f"[{datetime.now()}] 🟢 Starting Telegram client...")
    await tg_client.start(bot_token=BOT_TOKEN)
    print(f"[{datetime.now()}] 🟢 Creating Binance AsyncClient...")
    client = await AsyncClient.create(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

    # fetch real USDT pairs from exchange info
    try:
        info = await client.get_exchange_info()
        all_pairs = [s["symbol"] for s in info.get("symbols", []) if s["symbol"].endswith("USDT")]
        # convert to base symbols like 'BTC' from 'BTCUSDT'
        all_base_symbols = [p[:-4] for p in all_pairs]
        print(f"[{datetime.now()}] ℹ Found {len(all_base_symbols)} USDT pairs")
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Failed to fetch exchange info: {e}")
        # fallback: empty list -> exit
        await client.close_connection()
        raise

    # start background tasks
    asyncio.create_task(safe_poll_loop(client, all_base_symbols))
    asyncio.create_task(tp_watcher_loop())
    asyncio.create_task(reset_daily_signals_loop())

    print(f"[{datetime.now()}] 🟢 All background tasks started")
    await tg_client.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted, exiting...")
