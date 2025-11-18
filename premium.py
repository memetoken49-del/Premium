#!/usr/bin/env python3
import os
import asyncio
import threading
import json
from datetime import datetime, timezone, timedelta
from collections import deque

from flask import Flask
from binance import AsyncClient, BinanceSocketManager
from telethon import TelegramClient, events
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
client = TelegramClient("pre_pump_ws_session", API_ID, API_HASH)

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
# FILTER HELPERS
# -----------------------------
STABLECOINS = ["USDT", "BUSD", "USDC", "DAI", "TUSD"]
MIN_VOLUME = 500
VOLUME_SPIKE_THRESHOLD = 1.5
PRICE_CHANGE_THRESHOLD = 0.5

symbol_data = {}  # store deque of last trades per symbol

def is_stable(symbol):
    return any(sc in symbol.upper() for sc in STABLECOINS)

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
            "buy_price": price,
            "sell_targets": sells,
            "posted_at": now_iso,
            "posted_by": "bot"
        }
        upstash_set(key, payload)
        upstash_sadd_setname("active_signals", symbol)
    print(f"[{datetime.now()}] ✅ Signal posted: {symbol} at {price}")

# -----------------------------
# MONITOR TRADES VIA WEBSOCKET
# -----------------------------
async def monitor_trades(client_ws):
    global symbol_data
    bm = BinanceSocketManager(client_ws)
    usdt_symbols = [s['symbol'] for s in await client_ws.get_all_tickers() if s['symbol'].endswith("USDT") and not is_stable(s['symbol'])]
    
    streams = [f"{symbol.lower()}@trade" for symbol in usdt_symbols]
    # Create multiplex socket
    async with bm.multiplex_socket(streams) as ms:
        async for msg in ms:
            if 'data' not in msg:
                continue
            data = msg['data']
            symbol = data['s'].replace("USDT","")
            price = float(data['p'])
            qty = float(data['q'])
            trade_value = price*qty
            if trade_value < MIN_VOLUME or is_stable(data['s']):
                continue
            if symbol not in symbol_data:
                symbol_data[symbol] = {"trades": deque(maxlen=30), "last_avg_price": price, "last_volume": qty}
            symbol_data[symbol]["trades"].append({"price": price, "qty": qty})
            trades = symbol_data[symbol]["trades"]
            volume_now = sum(t['price']*t['qty'] for t in trades)
            price_now = sum(t['price'] for t in trades)/len(trades)
            volume_spike = volume_now / (symbol_data[symbol]["last_volume"]+1e-6)
            price_change = ((price_now - symbol_data[symbol]["last_avg_price"])/symbol_data[symbol]["last_avg_price"])*100
            symbol_data[symbol]["last_volume"] = volume_now
            symbol_data[symbol]["last_avg_price"] = price_now
            if volume_spike >= VOLUME_SPIKE_THRESHOLD and price_change >= PRICE_CHANGE_THRESHOLD:
                await post_signal(symbol, price_now)

# -----------------------------
# TP WATCHER
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
                # get current price from Binance API
                price_data = await client_ws.get_symbol_ticker(symbol=f"{symbol}USDT")
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
# AUTO CLEAN 30 DAYS
# -----------------------------
async def auto_clean_loop():
    while True:
        symbols = upstash_smembers("active_signals") or []
        now = datetime.now(timezone.utc)
        for symbol in symbols:
            key = f"signal:{symbol}"
            data = upstash_get(key)
            if not data:
                upstash_srem_setname("active_signals", symbol)
                continue
            if isinstance(data, str):
                try: data = json.loads(data)
                except: data = {}
            posted_at = data.get("posted_at")
            if posted_at:
                posted_dt = datetime.fromisoformat(posted_at)
                if (now-posted_dt).days >= 30:
                    upstash_srem_setname("active_signals", symbol)
                    upstash_del(key)
                    print(f"[{datetime.now()}] 🧹 Auto-cleaned {symbol} after 30 days")
        await asyncio.sleep(3600)  # check every hour

# -----------------------------
# MANUAL /signal
# -----------------------------
@client.on(events.NewMessage(pattern="/signal"))
async def manual_trigger(event):
    if event.sender_id != ADMIN_ID:
        await event.reply("❌ You are not authorized.")
        return
    await event.reply("⏳ Manual scan started...")
    await monitor_trades(client_ws)  # can also trigger mini scan
    await event.reply("✅ Manual scan completed.")

# -----------------------------
# MAIN
# -----------------------------
async def main():
    global client_ws
    client_ws = await AsyncClient.create(BINANCE_API_KEY, BINANCE_API_SECRET)
    print("✅ Binance WebSocket client ready")
    asyncio.create_task(monitor_trades(client_ws))
    asyncio.create_task(tp_watcher_loop())
    asyncio.create_task(auto_clean_loop())
    await client.start(bot_token=BOT_TOKEN)
    print("✅ Pre-Pump WebSocket Bot live")
    await client.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    asyncio.run(main())
