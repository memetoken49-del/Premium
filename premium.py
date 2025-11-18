#!/usr/bin/env python3
import os
import asyncio
import threading
import json
from datetime import datetime, timezone, timedelta
from collections import deque

from flask import Flask
from telethon import TelegramClient, events
from binance import AsyncClient, BinanceSocketManager
import requests

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
client = TelegramClient("pre_pump_session", API_ID, API_HASH)

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
                requests.get(url, timeout=10)
                print(f"[{datetime.now()}] 🔁 Self-ping to {url}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Self-ping error: {e}")
        finally:
            import time; time.sleep(240)

# -----------------------------
# UPSTASH HELPERS
# -----------------------------
UP_HEADERS = {"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"}

def upstash_set(key: str, value) -> dict:
    url = f"{UPSTASH_REST_URL}/set/{key}"
    try:
        resp = requests.post(url, headers=UP_HEADERS, data=json.dumps(value), timeout=10)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash set error: {e}")
        return {"error": str(e)}

def upstash_get(key: str):
    url = f"{UPSTASH_REST_URL}/get/{key}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=10)
        data = resp.json()
        return data.get("result")
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash get error: {e}")
        return None

def upstash_del(key: str) -> dict:
    url = f"{UPSTASH_REST_URL}/del/{key}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=10)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash del error: {e}")
        return {"error": str(e)}

def upstash_sadd_setname(setname: str, member: str) -> dict:
    url = f"{UPSTASH_REST_URL}/sadd/{setname}/{member}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=10)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash sadd error: {e}")
        return {"error": str(e)}

def upstash_srem_setname(setname: str, member: str) -> dict:
    url = f"{UPSTASH_REST_URL}/srem/{setname}/{member}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=10)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash srem error: {e}")
        return {"error": str(e)}

def upstash_smembers(setname: str):
    url = f"{UPSTASH_REST_URL}/smembers/{setname}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=10)
        js = resp.json()
        return js.get("result") or []
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash smembers error: {e}")
        return []

# -----------------------------
# HELPER FUNCTIONS
# -----------------------------
def is_stable(symbol):
    return any(s in symbol.upper() for s in ["USDT","BUSD","USDC","DAI","TUSD"])

def calculate_buy_sell_zones(price):
    percentages = [0.05, 0.12, 0.20, 0.35, 0.55, 0.85, 1.00]
    sell_zones = [round(price*(1+x), 6) for x in percentages]
    buy_zone_1 = round(price*0.98, 6)
    buy_zone_2 = round(price*0.995 * 1.015, 6)
    return buy_zone_1, buy_zone_2, sell_zones

# -----------------------------
# SIGNAL POSTING
# -----------------------------
async def post_signal(symbol, price):
    key = f"signal:{symbol}"
    buy1, buy2, sells = calculate_buy_sell_zones(price)
    msg = f"🚀 Binance\n#{symbol}/USDT\nBuy zone {buy1}-{buy2}\nSell zone {' - '.join([str(sz) for sz in sells])}\nMargin 3x"
    sent = await client.send_message(CHANNEL_ID, msg)
    msg_id = getattr(sent, "id", None)
    now_iso = datetime.now(timezone.utc).isoformat()

    existing = upstash_get(key)
    if existing:
        if isinstance(existing, str):
            try: existing = json.loads(existing)
            except: existing = {}
        msg_ids = existing.get("msg_ids", [])
        msg_ids.append({"msg_id": msg_id, "posted_at": now_iso})
        existing.update({"msg_ids": msg_ids, "buy_price": price, "sell_targets": sells, "posted_at": now_iso})
        upstash_set(key, existing)
    else:
        payload = {
            "msg_ids":[{"msg_id": msg_id,"posted_at": now_iso}],
            "symbol": symbol,
            "full_symbol": symbol+"USDT",
            "buy_price": price,
            "sell_targets": sells,
            "posted_at": now_iso,
            "posted_by": "bot"
        }
        upstash_set(key, payload)
        upstash_sadd_setname("active_signals", symbol)
    print(f"[{datetime.now()}] ✅ Posted {symbol} (msg_id={msg_id})")

# -----------------------------
# WEBSOCKET COIN MONITOR
# -----------------------------
volume_window = {}
VOLUME_WINDOW_SIZE = 10
VOLUME_SPIKE_MULTIPLIER = 1.5
PRICE_CHANGE_THRESHOLD = 0.3  # % price move for micro-pump

async def monitor_trades():
    client_ws = await AsyncClient.create(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    bsm = BinanceSocketManager(client_ws)
    stream = bsm.trade_socket('!miniTicker@arr')  # all symbols
    async with stream as tscm:
        async for msg in tscm:
            symbol = msg['s']
            if is_stable(symbol): continue

            price = float(msg['c'])
            qty = float(msg['v'])

            # Track volume
            if symbol not in volume_window:
                volume_window[symbol] = deque(maxlen=VOLUME_WINDOW_SIZE)
            vol_deque = volume_window[symbol]
            avg_volume = sum(vol_deque)/len(vol_deque) if vol_deque else qty
            vol_deque.append(qty)

            key = f"signal:{symbol}"
            prev = upstash_get(key)
            price_before = float(prev.get("buy_price", price)) if prev else price
            price_change = ((price - price_before)/price_before)*100

            if price_change >= PRICE_CHANGE_THRESHOLD and qty >= avg_volume * VOLUME_SPIKE_MULTIPLIER:
                await post_signal(symbol, price)

# -----------------------------
# TP WATCHER LOOP
# -----------------------------
async def tp_watcher_loop(poll_interval=60):
    while True:
        symbols = upstash_smembers("active_signals") or []
        for symbol in list(symbols):
            key = f"signal:{symbol}"
            data = upstash_get(key)
            if not data:
                upstash_srem_setname("active_signals", symbol)
                continue
            if isinstance(data,str):
                try: data = json.loads(data)
                except: data = {}
            current_price = float(data.get("buy_price", 0))  # fallback
            sell_targets = data.get("sell_targets", [])
            hit_index = None
            for idx, t in enumerate(sell_targets):
                if current_price >= float(t):
                    hit_index = idx
                    break
            if hit_index is not None:
                profit_pct = ((float(sell_targets[hit_index])-float(data.get("buy_price")))/float(data.get("buy_price")))*100*3
                msg = f"#{symbol}/USDT Take-Profit target {hit_index+1} ✅\nProfit: {profit_pct:.4f}% 📈"
                await client.send_message(CHANNEL_ID, msg)
                new_targets = sell_targets[hit_index+1:]
                if new_targets:
                    data["sell_targets"] = new_targets
                    upstash_set(key, data)
                else:
                    upstash_srem_setname("active_signals", symbol)
                    upstash_del(key)
        await asyncio.sleep(poll_interval)

# -----------------------------
# MANUAL SCAN COMMAND
# -----------------------------
@client.on(events.NewMessage(pattern="/signal"))
async def manual_trigger(event):
    if event.sender_id != ADMIN_ID:
        await event.reply("❌ You are not authorized.")
        return
    await event.reply("⏳ Manual scan started...")
    # WebSocket handles live signals; manual trigger just notifies
    await event.reply("✅ Manual scan completed.")

# -----------------------------
# MAIN
# -----------------------------
async def main():
    await client.start(bot_token=BOT_TOKEN)
    print("✅ Pre-Pump Scanner Bot live")
    asyncio.create_task(monitor_trades())
    asyncio.create_task(tp_watcher_loop())
    await client.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    asyncio.run(main())
