#!/usr/bin/env python3
import os
import asyncio
import threading
import json
import time
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask
from telethon import TelegramClient, events
from binance.client import Client

# -----------------------------
# ENVIRONMENT VARIABLES
# -----------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

UPSTASH_REST_URL = os.getenv("UPSTASH_REST_URL")
UPSTASH_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_TOKEN")
UP_HEADERS = {"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"}

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
                requests.get(url, timeout=10)
                print(f"[{datetime.now()}] 🔁 Self-ping to {url}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Self-ping error: {e}")
        time.sleep(240)

# -----------------------------
# Upstash Helpers
# -----------------------------
def upstash_set(key, value):
    try:
        resp = requests.post(f"{UPSTASH_REST_URL}/set/{key}", headers=UP_HEADERS, data=json.dumps(value), timeout=10)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash set error: {e}")
        return {"error": str(e)}

def upstash_get(key):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/get/{key}", headers=UP_HEADERS, timeout=10)
        data = resp.json()
        return data.get("result")
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash get error: {e}")
        return None

def upstash_del(key):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/del/{key}", headers=UP_HEADERS, timeout=10)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash del error: {e}")
        return {"error": str(e)}

def upstash_sadd_setname(setname, member):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/sadd/{setname}/{member}", headers=UP_HEADERS, timeout=10)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash sadd error: {e}")
        return {"error": str(e)}

def upstash_srem_setname(setname, member):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/srem/{setname}/{member}", headers=UP_HEADERS, timeout=10)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash srem error: {e}")
        return {"error": str(e)}

def upstash_smembers(setname):
    try:
        resp = requests.get(f"{UPSTASH_REST_URL}/smembers/{setname}", headers=UP_HEADERS, timeout=10)
        js = resp.json()
        return js.get("result") or []
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash smembers error: {e}")
        return []

# -----------------------------
# Helper Functions
# -----------------------------
def calculate_buy_sell_zones(price):
    percentages = [0.05, 0.12, 0.20, 0.35, 0.55, 0.85, 1.00]
    sell_zones = [round(price*(1+x), 6) for x in percentages]
    buy_zone_1 = round(price*0.98, 6)
    buy_zone_2 = round(price*0.995*1.015, 6)
    return buy_zone_1, buy_zone_2, sell_zones

def get_usdt_symbols():
    try:
        info = binance_client.get_exchange_info()
        return [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == "USDT" and s['status'] == "TRADING"]
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error fetching Binance symbols: {e}")
        return []

def get_current_price(symbol):
    try:
        data = binance_client.get_symbol_ticker(symbol=symbol)
        return float(data['price'])
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Binance GET price error for {symbol}: {e}")
        return None

# -----------------------------
# Signal Posting
# -----------------------------
async def post_signal(c):
    symbol = c["symbol"]
    price = c["current_price"]
    buy1, buy2, sells = calculate_buy_sell_zones(price)

    msg = f"🚀 Binance\n#{symbol}/USDT\nBuy zone {buy1}-{buy2}\nSell zone " + " - ".join([str(sz) for sz in sells]) + "\nMargin 3x"

    sent = await client.send_message(CHANNEL_ID, msg)
    msg_id = getattr(sent, "id", None)
    now_iso = datetime.now(timezone.utc).isoformat()

    key = f"signal:{symbol}"
    existing = upstash_get(key)
    if existing:
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except:
                existing = {}
        msg_ids = existing.get("msg_ids", [])
        msg_ids.append({"msg_id": msg_id, "posted_at": now_iso})
        existing.update({"msg_ids": msg_ids, "buy_price": price, "sell_targets": sells, "posted_at": now_iso})
        upstash_set(key, existing)
    else:
        payload = {"msg_ids":[{"msg_id":msg_id,"posted_at":now_iso}], "symbol":symbol, "coin_id":symbol, "buy_price":price, "sell_targets":sells, "posted_at":now_iso, "posted_by":"bot"}
        upstash_set(key, payload)
        upstash_sadd_setname("active_signals", symbol)

# -----------------------------
# Pre-Pump Scan
# -----------------------------
async def scan_and_post(auto=False):
    symbols = get_usdt_symbols()
    candidates = []

    for s in symbols:
        if any(stable in s for stable in ["USDT", "BUSD", "USDC", "DAI"]):
            continue

        price_now = get_current_price(s)
        if price_now is None:
            continue

        try:
            klines = binance_client.get_klines(symbol=s, interval=Client.KLINE_INTERVAL_1MINUTE, limit=15)
            price_earlier = float(klines[0][4])
            volume_earlier = float(klines[0][5])
            volume_now = float(klines[-1][5])
        except:
            continue

        change_short = ((price_now - price_earlier)/price_earlier)*100
        volume_spike = volume_now/(volume_earlier+1e-6)

        if volume_now<500 or volume_spike<1.5 or change_short<0.5:
            continue

        candidates.append({"symbol":s,"current_price":price_now,"short_term_change":change_short,"short_term_volume_ratio":volume_spike})

    if candidates:
        candidates.sort(key=lambda x:x["short_term_change"], reverse=True)
        await post_signal(candidates[0])
    else:
        msg = "❌ No early pump candidates found."
        suffix = "(auto scan)" if auto else "(manual scan)"
        await client.send_message(ADMIN_ID, msg+" "+suffix)

# -----------------------------
# TP Watcher
# -----------------------------
async def tp_watcher_loop(poll_interval=60):
    while True:
        try:
            symbols = upstash_smembers("active_signals") or []
            if not symbols:
                await asyncio.sleep(poll_interval)
                continue

            for symbol in symbols:
                key = f"signal:{symbol}"
                data = upstash_get(key)
                if not data:
                    upstash_srem_setname("active_signals", symbol)
                    continue

                if isinstance(data,str):
                    try:
                        data=json.loads(data)
                    except:
                        continue

                current_price = get_current_price(symbol)
                if current_price is None:
                    continue

                buy_price = float(data.get("buy_price"))
                sell_targets = data.get("sell_targets",[])

                if not sell_targets:
                    upstash_srem_setname("active_signals", symbol)
                    upstash_del(key)
                    continue

                hit_index = None
                for idx, t in enumerate(sell_targets):
                    t_float = float(t)
                    if current_price >= t_float:
                        hit_index = idx
                        break

                if hit_index is not None:
                    target_price = float(sell_targets[hit_index])
                    profit_pct = ((target_price-buy_price)/buy_price)*100
                    leverage_profit = profit_pct*3
                    msg = f"Binance\n#{symbol}/USDT Take-Profit target {hit_index+1} ✅\nProfit: {leverage_profit:.4f}% 📈"

                    await client.send_message(CHANNEL_ID,msg)

                    new_targets = sell_targets[hit_index+1:]
                    if new_targets:
                        data["sell_targets"]=new_targets
                        upstash_set(key,data)
                    else:
                        upstash_srem_setname("active_signals",symbol)
                        upstash_del(key)
        except Exception as e:
            print(f"[{datetime.now()}] ❌ TP watcher error: {e}")
        await asyncio.sleep(poll_interval)

# -----------------------------
# Manual /signal command
# -----------------------------
@client.on(events.NewMessage(pattern="/signal"))
async def manual_trigger(event):
    if event.sender_id != ADMIN_ID:
        await event.reply("❌ Not authorized")
        return
    await event.reply("⏳ Manual scan started")
    await scan_and_post(auto=False)
    await event.reply("✅ Manual scan completed")

# -----------------------------
# Auto scan loop
# -----------------------------
async def auto_scan_loop():
    while True:
        await scan_and_post(auto=True)
        await asyncio.sleep(600)

# -----------------------------
# Main
# -----------------------------
async def main():
    await client.start(bot_token=BOT_TOKEN)
    asyncio.create_task(tp_watcher_loop())
    asyncio.create_task(auto_scan_loop())
    print("✅ Bot is live")
    await client.run_until_disconnected()

if __name__=="__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    asyncio.run(main())
