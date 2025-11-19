#!/usr/bin/env python3
# premium_rest_safe.py - REST-only Binance Pre-Pump Scanner (Upstash + TP watcher)

import os
import asyncio
import threading
import json
import time
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import List

import requests
from flask import Flask
from telethon import TelegramClient, events
from binance import AsyncClient

# -----------------------------
# ENVIRONMENT VARIABLES
# -----------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
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
        try:
            url = os.environ.get("RENDER_URL")
            if url:
                try:
                    requests.get(url, timeout=10)
                except Exception as e:
                    print(f"[{datetime.now()}] ❌ Self-ping error: {e}")
                else:
                    print(f"[{datetime.now()}] 🔁 Self-ping to {url}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Self-ping outer error: {e}")
        time.sleep(240)

# -----------------------------
# UPSTASH HELPERS (sync)
# -----------------------------
UP_HEADERS = {"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"}

def upstash_set_sync(key: str, value) -> dict:
    url = f"{UPSTASH_REST_URL}/set/{key}"
    try:
        resp = requests.post(url, headers=UP_HEADERS, data=json.dumps(value), timeout=12)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash set error for {key}: {e}")
        return {"error": str(e)}

def upstash_get_sync(key: str):
    url = f"{UPSTASH_REST_URL}/get/{key}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        data = resp.json()
        return data.get("result")
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash get error for {key}: {e}")
        return None

def upstash_sadd_sync(setname: str, member: str) -> dict:
    url = f"{UPSTASH_REST_URL}/sadd/{setname}/{member}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash sadd error for {e}")
        return {"error": str(e)}

def upstash_srem_sync(setname: str, member: str) -> dict:
    url = f"{UPSTASH_REST_URL}/srem/{setname}/{member}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash srem error for {e}")
        return {"error": str(e)}

def upstash_smembers_sync(setname: str):
    url = f"{UPSTASH_REST_URL}/smembers/{setname}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        js = resp.json()
        return js.get("result") or []
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash smembers error: {e}")
        return []

# Async wrappers
async def upstash_set(key: str, value):
    return await asyncio.to_thread(upstash_set_sync, key, value)

async def upstash_get(key: str):
    return await asyncio.to_thread(upstash_get_sync, key)

async def upstash_sadd(setname: str, member: str):
    return await asyncio.to_thread(upstash_sadd_sync, setname, member)

async def upstash_srem(setname: str, member: str):
    return await asyncio.to_thread(upstash_srem_sync, setname, member)

async def upstash_smembers(setname: str):
    return await asyncio.to_thread(upstash_smembers_sync, setname)

# -----------------------------
# FILTER / STATE
# -----------------------------
MIN_TRADE_USD = float(os.getenv("MIN_TRADE_USD", "500.0"))
VOLUME_SPIKE_THRESHOLD = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "1.5"))
PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", "0.5"))  # percent
TRADE_WINDOW_SIZE = int(os.getenv("TRADE_WINDOW_SIZE", "30"))

symbol_state = {}  # symbol -> {trades deque, last_avg_price, last_volume}

def calculate_buy_sell_zones(price: float):
    percentages = [0.05, 0.12, 0.20, 0.35, 0.55, 0.85, 1.00]
    sell_zones = [round(price*(1+x), 6) for x in percentages]
    buy_zone_1 = round(price*0.98, 6)
    buy_zone_2 = round(price*0.995 * 1.015, 6)
    return buy_zone_1, buy_zone_2, sell_zones

# -----------------------------
# SIGNAL POSTING
# -----------------------------
async def post_signal(symbol_short: str, price: float):
    symbol = symbol_short.upper()
    key = f"signal:{symbol}"
    buy1, buy2, sells = calculate_buy_sell_zones(price)

    msg = f"🚀 Binance\n#{symbol}/USDT\nBuy zone {buy1}-{buy2}\nSell zone {' - '.join([str(sz) for sz in sells])}\nMargin 3x"
    try:
        sent = await tg_client.send_message(CHANNEL_ID, msg)
        msg_id = getattr(sent, "id", None)
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Telegram post failed for {symbol}: {e}")
        msg_id = None

    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "msg_ids": [{"msg_id": msg_id, "posted_at": now_iso}],
        "symbol": symbol,
        "buy_price": price,
        "sell_targets": sells,
        "posted_at": now_iso,
        "posted_by": "bot"
    }

    existing = await upstash_get(key)
    if existing:
        try:
            if isinstance(existing, str):
                existing = json.loads(existing)
        except:
            existing = {}
        msg_ids = existing.get("msg_ids", [])
        msg_ids.append({"msg_id": msg_id, "posted_at": now_iso})
        existing.update({"msg_ids": msg_ids, "buy_price": price, "sell_targets": sells, "posted_at": now_iso})
        await upstash_set(key, existing)
    else:
        await upstash_set(key, payload)
        await upstash_sadd("active_signals", symbol)

    await upstash_set(f"last_price:{symbol}", {"price": price, "updated_at": now_iso})
    print(f"[{datetime.now()}] ✅ Posted signal {symbol} at {price}")

# -----------------------------
# REST polling loop
# -----------------------------
async def poll_prices_loop(client_ws: AsyncClient, poll_interval=60):
    print(f"[{datetime.now()}] ⏱ Polling loop started (REST only, interval={poll_interval}s)")
    while True:
        try:
            tickers = await client_ws.get_all_tickers()  # weight=2
            for t in tickers:
                s = t.get("symbol", "")
                if not s.endswith("USDT"):
                    continue
                symbol_short = s.replace("USDT", "")
                price = float(t.get("price", 0))
                # update in-memory state
                state = symbol_state.get(symbol_short)
                if state is None:
                    state = {
                        "trades": deque(maxlen=TRADE_WINDOW_SIZE),
                        "last_avg_price": price,
                        "last_volume": 1.0
                    }
                    symbol_state[symbol_short] = state
                state["trades"].append({"price": price, "qty": 1.0})  # dummy qty
                # calculate metrics
                trades = state["trades"]
                volume_now = sum(t['price']*t['qty'] for t in trades)
                price_now = sum(t['price'] for t in trades)/len(trades) if trades else price
                prev_volume = state.get("last_volume", 1.0)
                prev_price = state.get("last_avg_price", price_now)
                volume_spike = volume_now / (prev_volume + 1e-9)
                price_change = ((price_now - prev_price)/(prev_price + 1e-9))*100
                state["last_volume"] = volume_now
                state["last_avg_price"] = price_now
                # detect pump
                if volume_spike >= VOLUME_SPIKE_THRESHOLD and price_change >= PRICE_CHANGE_THRESHOLD:
                    existing = await upstash_get(f"signal:{symbol_short}")
                    recent_posted = False
                    if existing:
                        try:
                            if isinstance(existing, str):
                                existing = json.loads(existing)
                            posted_at = existing.get("posted_at")
                            if posted_at:
                                dt = datetime.fromisoformat(posted_at)
                                if (datetime.now(timezone.utc)-dt).total_seconds()<600:
                                    recent_posted = True
                        except:
                            recent_posted = False
                    if not recent_posted:
                        asyncio.create_task(post_signal(symbol_short, price_now))
                await upstash_set(f"last_price:{symbol_short}", {"price": price_now, "updated_at": datetime.now(timezone.utc).isoformat()})
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Poll loop error: {e}")
        await asyncio.sleep(poll_interval)

# -----------------------------
# TP WATCHER LOOP
# -----------------------------
async def tp_watcher_loop(poll_interval=60):
    print(f"[{datetime.now()}] ⏱ TP watcher started (interval={poll_interval}s)")
    while True:
        try:
            symbols = await upstash_smembers("active_signals") or []
            for symbol in symbols:
                key = f"signal:{symbol}"
                data = await upstash_get(key)
                if not data:
                    await upstash_srem("active_signals", symbol)
                    continue
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except:
                        data = {}
                lp = await upstash_get(f"last_price:{symbol}")
                if lp and isinstance(lp, dict):
                    try:
                        current_price = float(lp.get("price"))
                    except:
                        continue
                else:
                    continue
                buy_price = float(data.get("buy_price", 0))
                sell_targets = data.get("sell_targets", []) or []
                hit_index = None
                for idx, t in enumerate(sell_targets):
                    if current_price >= float(t):
                        hit_index = idx
                        break
                if hit_index is not None:
                    profit_pct = ((float(sell_targets[hit_index]) - buy_price)/buy_price)*100*3.0
                    msg = f"#{symbol}/USDT Take-Profit target {hit_index+1} ✅\nProfit: {profit_pct:.4f}% 📈"
                    try:
                        msgs = data.get("msg_ids", [])
                        original_msg_id = msgs[-1].get("msg_id") if msgs else None
                        if original_msg_id:
                            await tg_client.send_message(CHANNEL_ID, msg, reply_to=original_msg_id)
                        else:
                            await tg_client.send_message(CHANNEL_ID, msg)
                    except Exception as e:
                        print(f"[{datetime.now()}] ❌ Failed TP msg for {symbol}: {e}")
                    new_targets = sell_targets[hit_index+1:]
                    if new_targets:
                        data["sell_targets"] = new_targets
                        await upstash_set(key, data)
                    else:
                        await upstash_srem("active_signals", symbol)
                        await upstash_set(key, None)
        except Exception as e:
            print(f"[{datetime.now()}] ❌ TP watcher error: {e}")
        await asyncio.sleep(poll_interval)

# -----------------------------
# MAIN STARTUP
# -----------------------------
async def main():
    await tg_client.start(bot_token=BOT_TOKEN)
    print(f"[{datetime.now()}] ✅ Telegram client started")
    client_ws = await AsyncClient.create(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    print(f"[{datetime.now()}] ✅ Binance AsyncClient created")
    asyncio.create_task(poll_prices_loop(client_ws, poll_interval=60))
    asyncio.create_task(tp_watcher_loop(poll_interval=60))
    print(f"[{datetime.now()}] 🟢 Bot fully started — REST-only mode")
    await tg_client.run_until_disconnected()

# -----------------------------
# ENTRYPOINT
# -----------------------------
if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted, exiting...")
