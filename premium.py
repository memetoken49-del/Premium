#!/usr/bin/env python3
# premium_rest_safe_optimized.py - REST-only Binance Pre-Pump Scanner (Upstash + TP watcher)

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
# UPSTASH HELPERS
# -----------------------------
UP_HEADERS = {"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"}

def upstash_set_sync(key, value):
    try:
        return requests.post(f"{UPSTASH_REST_URL}/set/{key}", headers=UP_HEADERS, data=json.dumps(value), timeout=12).json()
    except: return {}

def upstash_get_sync(key):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/get/{key}", headers=UP_HEADERS, timeout=12).json()
        return resp.get("result")
    except: return None

def upstash_sadd_sync(setname, member):
    try: return requests.get(f"{UPSTASH_REST_URL}/sadd/{setname}/{member}", headers=UP_HEADERS, timeout=12).json()
    except: return {}

def upstash_srem_sync(setname, member):
    try: return requests.get(f"{UPSTASH_REST_URL}/srem/{setname}/{member}", headers=UP_HEADERS, timeout=12).json()
    except: return {}

def upstash_smembers_sync(setname):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/smembers/{setname}", headers=UP_HEADERS, timeout=12).json()
        return resp.get("result") or []
    except: return []

# Async wrappers
async def upstash_set(key, value): return await asyncio.to_thread(upstash_set_sync, key, value)
async def upstash_get(key): return await asyncio.to_thread(upstash_get_sync, key)
async def upstash_sadd(setname, member): return await asyncio.to_thread(upstash_sadd_sync, setname, member)
async def upstash_srem(setname, member): return await asyncio.to_thread(upstash_srem_sync, setname, member)
async def upstash_smembers(setname): return await asyncio.to_thread(upstash_smembers_sync, setname)

# -----------------------------
# FILTER / STATE
# -----------------------------
MIN_TRADE_USD = float(os.getenv("MIN_TRADE_USD", "500.0"))
VOLUME_SPIKE_THRESHOLD = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "1.5"))
PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", "0.5"))
TRADE_WINDOW_SIZE = int(os.getenv("TRADE_WINDOW_SIZE", "30"))

symbol_state = {}  # symbol -> {trades deque, last_avg_price, last_volume}

def calculate_buy_sell_zones(price: float):
    sell_perc = [0.05, 0.12, 0.20, 0.35, 0.55, 0.85, 1.00]
    sell_zones = [round(price*(1+x), 6) for x in sell_perc]
    buy_zone_1 = round(price*0.98, 6)
    buy_zone_2 = round(price*0.995 * 1.015, 6)
    return buy_zone_1, buy_zone_2, sell_zones

# -----------------------------
# SIGNAL POSTING
# -----------------------------
async def post_signal(symbol_short, price):
    symbol = symbol_short.upper()
    key = f"signal:{symbol}"
    buy1, buy2, sells = calculate_buy_sell_zones(price)
    msg = f"🚀 Binance\n#{symbol}/USDT\nBuy zone {buy1}-{buy2}\nSell zone {' - '.join([str(sz) for sz in sells])}\nMargin 3x"
    try:
        sent = await tg_client.send_message(CHANNEL_ID, msg)
        msg_id = getattr(sent, "id", None)
    except: msg_id = None

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
        try: existing = json.loads(existing) if isinstance(existing, str) else existing
        except: existing = {}
        msgs = existing.get("msg_ids", [])
        msgs.append({"msg_id": msg_id, "posted_at": now_iso})
        existing.update({"msg_ids": msgs, "buy_price": price, "sell_targets": sells, "posted_at": now_iso})
        await upstash_set(key, existing)
    else:
        await upstash_set(key, payload)
        await upstash_sadd("active_signals", symbol)
    await upstash_set(f"last_price:{symbol}", {"price": price, "updated_at": now_iso})
    print(f"[{datetime.now()}] ✅ Posted signal {symbol} at {price}")

# -----------------------------
# DYNAMIC POLL INTERVAL
# -----------------------------
COINS_COUNT = 440
UPSTASH_MONTH_LIMIT = 500_000
DAYS_IN_MONTH = 30

max_polls_month = UPSTASH_MONTH_LIMIT // COINS_COUNT
polls_per_day = max_polls_month / DAYS_IN_MONTH
POLL_INTERVAL_SECONDS = int(24*60*60 / polls_per_day)  # ≈ 38 minutes

# -----------------------------
# REST POLLING LOOP
# -----------------------------
async def poll_prices_loop(client_ws):
    print(f"[{datetime.now()}] ⏱ Polling loop started (interval={POLL_INTERVAL_SECONDS}s)")
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
                    existing = await upstash_get(f"signal:{symbol}")
                    recent_posted=False
                    if existing:
                        try:
                            if isinstance(existing,str): existing=json.loads(existing)
                            posted_at = existing.get("posted_at")
                            if posted_at:
                                dt = datetime.fromisoformat(posted_at)
                                if (datetime.now(timezone.utc)-dt).total_seconds()<600: recent_posted=True
                        except: recent_posted=False
                    if not recent_posted: asyncio.create_task(post_signal(symbol,price_now))
                await upstash_set(f"last_price:{symbol}", {"price":price_now,"updated_at":datetime.now(timezone.utc).isoformat()})
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Poll loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

# -----------------------------
# TP WATCHER LOOP
# -----------------------------
async def tp_watcher_loop():
    print(f"[{datetime.now()}] ⏱ TP watcher started")
    while True:
        try:
            symbols = await upstash_smembers("active_signals") or []
            for symbol in symbols:
                key=f"signal:{symbol}"
                data = await upstash_get(key)
                if not data: await upstash_srem("active_signals",symbol); continue
                if isinstance(data,str): 
                    try: data=json.loads(data)
                    except: data={}
                lp = await upstash_get(f"last_price:{symbol}")
                if lp and isinstance(lp,dict):
                    try: current_price=float(lp.get("price")); 
                    except: continue
                else: continue
                buy_price = float(data.get("buy_price",0))
                sell_targets = data.get("sell_targets",[]) or []
                hit_index=None
                for idx,t in enumerate(sell_targets):
                    if current_price>=float(t):
                        hit_index=idx
                        break
                if hit_index is not None:
                    profit_pct=((float(sell_targets[hit_index])-buy_price)/buy_price)*100*3.0
                    msg=f"#{symbol}/USDT TP {hit_index+1} ✅\nProfit: {profit_pct:.4f}% 📈"
                    try:
                        msgs = data.get("msg_ids",[])
                        orig_msg_id = msgs[-1].get("msg_id") if msgs else None
                        if orig_msg_id: await tg_client.send_message(CHANNEL_ID,msg,reply_to=orig_msg_id)
                        else: await tg_client.send_message(CHANNEL_ID,msg)
                    except: pass
                    new_targets = sell_targets[hit_index+1:]
                    if new_targets: data["sell_targets"]=new_targets; await upstash_set(key,data)
                    else: await upstash_srem("active_signals",symbol); await upstash_set(key,None)
        except: pass
        await asyncio.sleep(60)

# -----------------------------
# MAIN
# -----------------------------
async def main():
    await tg_client.start(bot_token=BOT_TOKEN)
    client_ws = await AsyncClient.create(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    asyncio.create_task(poll_prices_loop(client_ws))
    asyncio.create_task(tp_watcher_loop())
    await tg_client.run_until_disconnected()

if __name__=="__main__":
    threading.Thread(target=run_web,daemon=True).start()
    threading.Thread(target=self_ping,daemon=True).start()
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Interrupted, exiting...")
