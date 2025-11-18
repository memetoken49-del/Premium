#!/usr/bin/env python3
import os
import asyncio
import threading
import json
from datetime import datetime, timezone, timedelta
from flask import Flask
from binance.client import Client
from telethon import TelegramClient, events
import requests
import time

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
            time.sleep(240)

# -----------------------------
# BINANCE CLIENT
# -----------------------------
binance_client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

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
# FETCH BINANCE COINS
# -----------------------------
async def fetch_binance_usdt_coins():
    binance_coins = []
    try:
        info = binance_client.get_all_tickers()
        for ticker in info:
            symbol = ticker['symbol']
            if symbol.endswith("USDT") and not is_stable(symbol):
                binance_coins.append({
                    "symbol": symbol.replace("USDT",""),
                    "full_symbol": symbol,
                    "current_price": float(ticker['price'])
                })
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error fetching Binance coins: {e}")
    return binance_coins

# -----------------------------
# POST SIGNAL
# -----------------------------
async def post_signal(c):
    symbol = c["symbol"].upper()
    full_symbol = c["full_symbol"]
    key = f"signal:{symbol}"
    price = c["current_price"]
    buy1, buy2, sells = calculate_buy_sell_zones(price)

    msg = f"🚀 Binance\n#{symbol}/USDT\nBuy zone {buy1}-{buy2}\nSell zone {' - '.join([str(sz) for sz in sells])}\nMargin 3x"
    sent = await client.send_message(CHANNEL_ID, msg)
    msg_id = getattr(sent, "id", None)
    now_iso = datetime.now(timezone.utc).isoformat()

    existing = upstash_get(key)
    if existing:
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except:
                existing = {}
        msg_ids = existing.get("msg_ids", [])
        msg_ids.append({"msg_id": msg_id, "posted_at": now_iso})
        existing.update({
            "msg_ids": msg_ids,
            "buy_price": price,
            "sell_targets": sells,
            "posted_at": now_iso
        })
        upstash_set(key, existing)
    else:
        payload = {
            "msg_ids": [{"msg_id": msg_id, "posted_at": now_iso}],
            "symbol": symbol,
            "full_symbol": full_symbol,
            "buy_price": price,
            "sell_targets": sells,
            "posted_at": now_iso,
            "posted_by": "bot"
        }
        upstash_set(key, payload)
        upstash_sadd_setname("active_signals", symbol)
    print(f"[{datetime.now()}] ✅ Posted {symbol} (msg_id={msg_id})")

# -----------------------------
# SCAN & POST LOGIC
# -----------------------------
async def scan_and_post(auto=False):
    coins = await fetch_binance_usdt_coins()
    candidates = []

    for c in coins:
        symbol = c["symbol"]
        try:
            klines = binance_client.get_klines(symbol=c["full_symbol"], interval="1m", limit=2)
            price_now = float(klines[-1][4])
            price_earlier = float(klines[0][4])
            change_short = ((price_now - price_earlier) / price_earlier) * 100

            volume_now = float(klines[-1][5])
            volume_earlier = float(klines[0][5])
            volume_spike = volume_now / (volume_earlier + 1e-6)

            if volume_now < 500 or volume_spike < 1.5 or change_short < 0.5:
                continue

            c["short_term_change"] = change_short
            c["short_term_volume_ratio"] = volume_spike
            c["current_price"] = price_now
            candidates.append(c)
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error fetching klines for {symbol}: {e}")
            continue

    candidates.sort(key=lambda x: x["short_term_change"], reverse=True)

    if not candidates:
        suffix = "(auto scan)" if auto else "(manual scan)"
        await client.send_message(ADMIN_ID, f"❌ No early pump candidates found. {suffix}")
        return

    await post_signal(candidates[0])

# -----------------------------
# AUTO SCAN LOOP
# -----------------------------
async def auto_scan_loop():
    while True:
        print(f"[{datetime.now()}] 🔍 Auto scan running...")
        await scan_and_post(auto=True)
        await asyncio.sleep(600)

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
            if isinstance(data, str):
                try: data = json.loads(data)
                except: data = {}
            try:
                price_data = binance_client.get_symbol_ticker(symbol=data["full_symbol"])
                current_price = float(price_data['price'])
            except:
                continue
            buy_price = float(data.get("buy_price"))
            sell_targets = data.get("sell_targets", [])
            hit_index = None
            for idx, t in enumerate(sell_targets):
                if current_price >= float(t):
                    hit_index = idx
                    break
            if hit_index is not None:
                profit_pct = ((float(sell_targets[hit_index])-buy_price)/buy_price)*100*3
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
# MONTHLY CLEANUP LOOP
# -----------------------------
async def monthly_cleanup_loop():
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == 0 and now.minute == 0:
            print(f"[{datetime.now()}] 🧹 Monthly cleanup running...")
            symbols = upstash_smembers("active_signals") or []
            for symbol in symbols:
                upstash_del(f"signal:{symbol}")
                upstash_srem_setname("active_signals", symbol)
            print(f"[{datetime.now()}] ✅ Monthly cleanup done for {len(symbols)} signals.")
            await asyncio.sleep(61)
        else:
            await asyncio.sleep(30)

# -----------------------------
# MANUAL SCAN COMMAND
# -----------------------------
@client.on(events.NewMessage(pattern="/signal"))
async def manual_trigger(event):
    if event.sender_id != ADMIN_ID:
        await event.reply("❌ You are not authorized.")
        return
    await event.reply("⏳ Manual scan started...")
    await scan_and_post(auto=False)
    await event.reply("✅ Manual scan completed.")

# -----------------------------
# MAIN
# -----------------------------
async def main():
    await client.start(bot_token=BOT_TOKEN)
    print("✅ Pre-Pump Scanner Bot live")
    asyncio.create_task(auto_scan_loop())
    asyncio.create_task(tp_watcher_loop())
    asyncio.create_task(monthly_cleanup_loop())
    await client.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    asyncio.run(main())
