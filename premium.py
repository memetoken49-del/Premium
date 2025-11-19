#!/usr/bin/env python3
# premium_full.py - Robust Pre-Pump Scanner Bot (WebSocket-only)

import os
import asyncio
import threading
import json
import time
from datetime import datetime, timezone, timedelta
from collections import deque

import requests
from flask import Flask
from telethon import TelegramClient, events
from binance import AsyncClient, BinanceSocketManager

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
                try: requests.get(url, timeout=10)
                except Exception as e: print(f"[{datetime.now()}] ❌ Self-ping error: {e}")
                else: print(f"[{datetime.now()}] 🔁 Self-ping to {url}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Self-ping outer error: {e}")
        time.sleep(240)

# -----------------------------
# UPSTASH HELPERS
# -----------------------------
UP_HEADERS = {"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"}

def upstash_set(key: str, value) -> dict:
    url = f"{UPSTASH_REST_URL}/set/{key}"
    try:
        resp = requests.post(url, headers=UP_HEADERS, data=json.dumps(value), timeout=12)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash set error for {key}: {e}")
        return {"error": str(e)}

def upstash_get(key: str):
    url = f"{UPSTASH_REST_URL}/get/{key}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        data = resp.json()
        return data.get("result")
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash get error for {key}: {e}")
        return None

def upstash_del(key: str) -> dict:
    url = f"{UPSTASH_REST_URL}/del/{key}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash del error for {key}: {e}")
        return {"error": str(e)}

def upstash_sadd_setname(setname: str, member: str) -> dict:
    url = f"{UPSTASH_REST_URL}/sadd/{setname}/{member}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash sadd error: {e}")
        return {"error": str(e)}

def upstash_srem_setname(setname: str, member: str) -> dict:
    url = f"{UPSTASH_REST_URL}/srem/{setname}/{member}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash srem error: {e}")
        return {"error": str(e)}

def upstash_smembers(setname: str):
    url = f"{UPSTASH_REST_URL}/smembers/{setname}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        js = resp.json()
        return js.get("result") or []
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash smembers error: {e}")
        return []

# -----------------------------
# FILTER / THRESHOLDS / STATE
# -----------------------------
MIN_TRADE_USD = 500.0
VOLUME_SPIKE_THRESHOLD = 1.5
PRICE_CHANGE_THRESHOLD = 0.5  # percent
TRADE_WINDOW_SIZE = 30

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

    existing = upstash_get(key)
    if existing:
        try:
            if isinstance(existing, str): existing = json.loads(existing)
        except: existing = {}
        msg_ids = existing.get("msg_ids", [])
        msg_ids.append({"msg_id": msg_id, "posted_at": now_iso})
        existing.update({"msg_ids": msg_ids, "buy_price": price, "sell_targets": sells, "posted_at": now_iso})
        upstash_set(key, existing)
    else:
        upstash_set(key, payload)
        upstash_sadd_setname("active_signals", symbol)

    upstash_set(f"last_price:{symbol}", {"price": price, "updated_at": now_iso})
    print(f"[{datetime.now()}] ✅ Posted signal {symbol} at {price}")

# -----------------------------
# ROBUST WEBSOCKET MONITOR
# -----------------------------
async def monitor_trades_ws(client_ws: AsyncClient):
    print(f"[{datetime.now()}] 🔌 Starting WebSocket monitor (all USDT pairs)")

    bsm = BinanceSocketManager(client_ws)

    # Get all USDT symbols once at start
    tickers = await client_ws.get_all_tickers()
    usdt_symbols = [t['symbol'] for t in tickers if t['symbol'].endswith("USDT")]

    streams = [f"{s.lower()}@trade" for s in usdt_symbols]

    async with bsm.multiplex_socket(streams) as ms:
        print(f"[{datetime.now()}] 🟢 WebSocket multiplex connected with {len(streams)} symbols")
        while True:
            try:
                msg = await ms.recv()
                if not msg:
                    await asyncio.sleep(0.01)
                    continue
                data = msg.get("data") or msg
                symbol_full = data.get("s")
                if not symbol_full:
                    continue

                price = float(data.get("p", 0))
                qty = float(data.get("q", 0))
                trade_value = price * qty
                if trade_value < MIN_TRADE_USD:
                    continue

                symbol_short = symbol_full.replace("USDT", "")
                state = symbol_state.get(symbol_short)
                if state is None:
                    state = {"trades": deque(maxlen=TRADE_WINDOW_SIZE), "last_avg_price": price, "last_volume": qty}
                    symbol_state[symbol_short] = state

                state["trades"].append({"price": price, "qty": qty})

                trades = state["trades"]
                volume_now = sum(t['price']*t['qty'] for t in trades)
                price_now = sum(t['price'] for t in trades)/len(trades) if trades else price
                prev_volume = state.get("last_volume", 1.0)
                prev_price = state.get("last_avg_price", price_now)
                volume_spike = volume_now / (prev_volume + 1e-9)
                price_change = ((price_now - prev_price)/(prev_price+1e-9))*100

                state["last_volume"] = volume_now
                state["last_avg_price"] = price_now

                # Trigger signal if spike detected
                if volume_spike >= VOLUME_SPIKE_THRESHOLD and price_change >= PRICE_CHANGE_THRESHOLD:
                    existing = upstash_get(f"signal:{symbol_short}")
                    recent_posted = False
                    if existing:
                        try:
                            if isinstance(existing,str): existing = json.loads(existing)
                            posted_at = existing.get("posted_at")
                            if posted_at:
                                dt = datetime.fromisoformat(posted_at)
                                if (datetime.now(timezone.utc) - dt).total_seconds() < 600:
                                    recent_posted = True
                        except: pass
                    if not recent_posted:
                        asyncio.create_task(post_signal(symbol_short, price_now))

                upstash_set(f"last_price:{symbol_short}", {"price": price_now, "updated_at": datetime.now(timezone.utc).isoformat()})

            except Exception as e:
                print(f"[{datetime.now()}] ⚠️ WebSocket message error: {e}")
                await asyncio.sleep(0.5)

# -----------------------------
# TP WATCHER LOOP
# -----------------------------
async def tp_watcher_loop(poll_interval=60):
    print(f"[{datetime.now()}] ⏱ TP watcher started")
    while True:
        try:
            symbols = upstash_smembers("active_signals") or []
            for symbol in list(symbols):
                key = f"signal:{symbol}"
                data = upstash_get(key)
                if not data:
                    upstash_srem_setname("active_signals", symbol)
                    continue
                if isinstance(data,str):
                    try:data=json.loads(data)
                    except: data={}
                lp = upstash_get(f"last_price:{symbol}")
                if lp and isinstance(lp,dict):
                    try: current_price=float(lp.get("price"))
                    except: continue
                else: continue
                buy_price=float(data.get("buy_price",0))
                sell_targets=data.get("sell_targets",[]) or []
                hit_index=None
                for idx,t in enumerate(sell_targets):
                    if current_price>=float(t):
                        hit_index=idx
                        break
                if hit_index is not None:
                    profit_pct=((float(sell_targets[hit_index])-buy_price)/buy_price)*100*3.0
                    posted_at=data.get("posted_at")
                    period_str="N/A"
                    try:
                        posted_dt=datetime.fromisoformat(posted_at)
                        delta=datetime.now(timezone.utc)-posted_dt
                        hours,rem=divmod(int(delta.total_seconds()),3600)
                        minutes,_=divmod(rem,60)
                        period_str=f"{hours} Hours {minutes} Minutes"
                    except: pass
                    msg=f"#{symbol}/USDT Take-Profit target {hit_index+1} ✅\nProfit: {profit_pct:.4f}% 📈\nPeriod: {period_str} ⏰\n"
                    try:
                        msgs=data.get("msg_ids",[])
                        original_msg_id=None
                        if msgs: original_msg_id=msgs[-1].get("msg_id")
                        if original_msg_id:
                            await tg_client.send_message(CHANNEL_ID,msg,reply_to=original_msg_id)
                        else: await tg_client.send_message(CHANNEL_ID,msg)
                    except Exception as e:
                        print(f"[{datetime.now()}] ❌ Failed TP msg for {symbol}: {e}")
                    new_targets=sell_targets[hit_index+1:]
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
# CLEANUP LOOP
# -----------------------------
async def cleanup_old_signals_loop(poll_interval=3600):
    print(f"[{datetime.now()}] 🧹 Auto-clean loop started")
    while True:
        try:
            symbols = upstash_smembers("active_signals") or []
            now=datetime.now(timezone.utc)
            for symbol in symbols:
                key=f"signal:{symbol}"
                data=upstash_get(key)
                if not data:
                    upstash_srem_setname("active_signals",symbol)
                    continue
                if isinstance(data,str):
                    try:data=json.loads(data)
                    except: data={}
                posted_at=data.get("posted_at")
                if posted_at:
                    try:
                        posted_dt=datetime.fromisoformat(posted_at)
                        if (now-posted_dt)>=timedelta(days=30):
                            upstash_del(key)
                            upstash_srem_setname("active_signals",symbol)
                            print(f"[{datetime.now()}] 🧹 Auto-cleaned {symbol} after 30 days")
                    except Exception as e:
                        print(f"[{datetime.now()}] ❌ Auto-clean parse error for {symbol}: {e}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Cleanup loop error: {e}")
        await asyncio.sleep(poll_interval)

# -----------------------------
# MANUAL /signal COMMAND
# -----------------------------
@tg_client.on(events.NewMessage(pattern="/signal"))
async def manual_trigger(event):
    if event.sender_id!=ADMIN_ID:
        await event.reply("❌ You are not authorized.")
        return
    await event.reply("⏳ Manual scan acknowledged — monitoring live WebSocket feed.")
    await event.reply("✅ Manual trigger done.")

# -----------------------------
# MAIN STARTUP
# -----------------------------
async def main():
    await tg_client.start(bot_token=BOT_TOKEN)
    print(f"[{datetime.now()}] ✅ Telegram client started")
    client_ws=await AsyncClient.create(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    print(f"[{datetime.now()}] ✅ Binance AsyncClient created")

    asyncio.create_task(monitor_trades_ws(client_ws))
    asyncio.create_task(tp_watcher_loop())
    asyncio.create_task(cleanup_old_signals_loop())

    print(f"[{datetime.now()}] 🟢 Bot fully started")
    await tg_client.run_until_disconnected()

# -----------------------------
# ENTRYPOINT
# -----------------------------
if __name__=="__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted, exiting...")
