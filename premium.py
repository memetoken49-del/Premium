#!/usr/bin/env python3
# premium_fixed.py - WebSocket-batched, non-blocking Pre-Pump Scanner (Upstash kept)

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
# UPSTASH HELPERS (sync) - keep synchronous requests
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

def upstash_del_sync(key: str) -> dict:
    url = f"{UPSTASH_REST_URL}/del/{key}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash del error for {key}: {e}")
        return {"error": str(e)}

def upstash_sadd_sync(setname: str, member: str) -> dict:
    url = f"{UPSTASH_REST_URL}/sadd/{setname}/{member}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash sadd error: {e}")
        return {"error": str(e)}

def upstash_srem_sync(setname: str, member: str) -> dict:
    url = f"{UPSTASH_REST_URL}/srem/{setname}/{member}"
    try:
        resp = requests.get(url, headers=UP_HEADERS, timeout=12)
        return resp.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Upstash srem error: {e}")
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

# Async wrappers to avoid blocking event loop
async def upstash_set(key: str, value):
    return await asyncio.to_thread(upstash_set_sync, key, value)

async def upstash_get(key: str):
    return await asyncio.to_thread(upstash_get_sync, key)

async def upstash_del(key: str):
    return await asyncio.to_thread(upstash_del_sync, key)

async def upstash_sadd(setname: str, member: str):
    return await asyncio.to_thread(upstash_sadd_sync, setname, member)

async def upstash_srem(setname: str, member: str):
    return await asyncio.to_thread(upstash_srem_sync, setname, member)

async def upstash_smembers(setname: str):
    return await asyncio.to_thread(upstash_smembers_sync, setname)

# -----------------------------
# FILTER / THRESHOLDS / STATE
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
# SIGNAL POSTING (non-blocking)
# -----------------------------
async def post_signal(symbol_short: str, price: float):
    """
    Posts to Telegram and updates Upstash. Runs as an async task.
    Uses upstash_set wrapper to avoid blocking.
    """
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

    # cache last price for TP watcher
    await upstash_set(f"last_price:{symbol}", {"price": price, "updated_at": now_iso})
    print(f"[{datetime.now()}] ✅ Posted signal {symbol} at {price}")

# -----------------------------
# Trade message processing (off main recv)
# -----------------------------
async def process_trade_message(msg):
    """
    Parse and process a single trade message.
    Runs as a separate task to keep recv() extremely fast.
    msg: multiplex format {'stream': ..., 'data': {...}} or direct trade dict.
    """
    try:
        data = msg.get("data") if isinstance(msg, dict) else msg
        # some multiplex returns {'stream', 'data'}
        if not data:
            return

        s = data.get("s") or data.get("symbol") or None
        if not s:
            return
        # We're monitoring USDT pairs (symbol endswith 'USDT')
        if not s.endswith("USDT"):
            return

        price = float(data.get("p", data.get("price", 0)))
        qty = float(data.get("q", data.get("qty", 0)))
        trade_value = price * qty
        if trade_value < MIN_TRADE_USD:
            return

        symbol_short = s.replace("USDT", "")

        # update fast in-memory state
        state = symbol_state.get(symbol_short)
        if state is None:
            state = {
                "trades": deque(maxlen=TRADE_WINDOW_SIZE),
                "last_avg_price": price,
                "last_volume": qty
            }
            symbol_state[symbol_short] = state

        state["trades"].append({"price": price, "qty": qty})
        trades = state["trades"]

        # compute aggregated metrics
        volume_now = sum(t['price'] * t['qty'] for t in trades)
        price_now = (sum(t['price'] for t in trades) / len(trades)) if trades else price

        prev_volume = state.get("last_volume", 1.0)
        prev_price = state.get("last_avg_price", price_now)

        volume_spike = volume_now / (prev_volume + 1e-9)
        price_change = ((price_now - prev_price) / (prev_price + 1e-9)) * 100

        state["last_volume"] = volume_now
        state["last_avg_price"] = price_now

        # detection
        if volume_spike >= VOLUME_SPIKE_THRESHOLD and price_change >= PRICE_CHANGE_THRESHOLD:
            # duplicate protection: check existing posted signal
            existing = await upstash_get(f"signal:{symbol_short}")
            recent_posted = False
            if existing:
                try:
                    if isinstance(existing, str):
                        existing = json.loads(existing)
                    posted_at = existing.get("posted_at")
                    if posted_at:
                        dt = datetime.fromisoformat(posted_at)
                        if (datetime.now(timezone.utc) - dt).total_seconds() < 600:
                            recent_posted = True
                except:
                    recent_posted = False
            if not recent_posted:
                # spawn post task (async)
                asyncio.create_task(post_signal(symbol_short, price_now))

        # update last price cache for TP watcher
        await upstash_set(f"last_price:{symbol_short}", {"price": price_now, "updated_at": datetime.now(timezone.utc).isoformat()})

    except Exception as exc:
        print(f"[{datetime.now()}] ⚠️ process_trade_message error: {exc}")

# -----------------------------
# Helper: chunk a list into batches
# -----------------------------
def chunk_list(lst: List[str], n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

# -----------------------------
# Run one socket batch (one multiplex connection)
# -----------------------------
async def run_socket_batch(client_ws: AsyncClient, symbols_batch: List[str], batch_index: int):
    """
    Each batch runs its own multiplex socket and hands off messages to process_trade_message.
    """
    try:
        streams = [f"{s.lower()}@trade" for s in symbols_batch]
        # increase max_queue_size to avoid quick overflows
        bsm = BinanceSocketManager(client_ws, user_timeout=60, max_queue_size=5000)
        async with bsm.multiplex_socket(streams) as ms:
            print(f"[{datetime.now()}] 🟢 Batch {batch_index}: connected with {len(streams)} streams")
            while True:
                try:
                    msg = await ms.recv()
                    if not msg:
                        await asyncio.sleep(0.001)
                        continue
                    # immediately delegate processing (non-blocking)
                    asyncio.create_task(process_trade_message(msg))
                except Exception as inner:
                    print(f"[{datetime.now()}] ⚠️ Batch {batch_index} message loop error: {inner}")
                    break
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Socket batch {batch_index} outer error: {e} — will retry in 3s")
        await asyncio.sleep(3)
        # let caller (monitor) respawn this batch if needed

# -----------------------------
# Monitor: start batches (calls get_all_tickers only once)
# -----------------------------
async def monitor_trades_ws(client_ws: AsyncClient, batch_size:int=20):
    """
    Fetch USDT symbols once and spawn multiple socket batches.
    """
    print(f"[{datetime.now()}] 🔌 Starting monitor: loading symbols once")
    try:
        tickers = await client_ws.get_all_tickers()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Failed to fetch tickers at startup: {e}")
        # backoff and retry
        await asyncio.sleep(5)
        return await monitor_trades_ws(client_ws, batch_size)

    usdt_symbols = [t['symbol'] for t in tickers if t['symbol'].endswith("USDT")]
    if not usdt_symbols:
        print(f"[{datetime.now()}] ⚠️ No USDT symbols found after startup call")
        await asyncio.sleep(10)
        return await monitor_trades_ws(client_ws, batch_size)

    print(f"[{datetime.now()}] 🟢 Loaded {len(usdt_symbols)} USDT symbols; batching size={batch_size}")
    batches = list(chunk_list(usdt_symbols, batch_size))

    # Start one task per batch. If a batch fails, monitor loop can restart tasks.
    for idx, batch in enumerate(batches):
        asyncio.create_task(run_socket_batch(client_ws, batch, idx+1))

    # Keep the monitor alive and optionally refresh symbol list periodically (every 6 hours)
    while True:
        # this loop simply keeps monitor alive; you may implement periodic refresh here
        await asyncio.sleep(3600)

# -----------------------------
# TP WATCHER LOOP
# -----------------------------
async def tp_watcher_loop(poll_interval=60):
    print(f"[{datetime.now()}] ⏱ TP watcher started (poll {poll_interval}s)")
    while True:
        try:
            symbols = await upstash_smembers("active_signals") or []
            for symbol in list(symbols):
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
                    profit_pct = ((float(sell_targets[hit_index]) - buy_price) / buy_price) * 100 * 3.0
                    posted_at = data.get("posted_at")
                    period_str = "N/A"
                    try:
                        posted_dt = datetime.fromisoformat(posted_at)
                        delta = datetime.now(timezone.utc) - posted_dt
                        hours, rem = divmod(int(delta.total_seconds()), 3600)
                        minutes, _ = divmod(rem, 60)
                        period_str = f"{hours} Hours {minutes} Minutes"
                    except:
                        pass
                    msg = f"#{symbol}/USDT Take-Profit target {hit_index+1} ✅\nProfit: {profit_pct:.4f}% 📈\nPeriod: {period_str} ⏰\n"
                    try:
                        msgs = data.get("msg_ids", [])
                        original_msg_id = None
                        if msgs:
                            original_msg_id = msgs[-1].get("msg_id")
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
                        await upstash_del(key)
        except Exception as e:
            print(f"[{datetime.now()}] ❌ TP watcher error: {e}")
        await asyncio.sleep(poll_interval)

# -----------------------------
# CLEANUP LOOP (30 days)
# -----------------------------
async def cleanup_old_signals_loop(poll_interval=3600):
    print(f"[{datetime.now()}] 🧹 Auto-clean loop started (every {poll_interval}s)")
    while True:
        try:
            symbols = await upstash_smembers("active_signals") or []
            now = datetime.now(timezone.utc)
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
                posted_at = data.get("posted_at")
                if posted_at:
                    try:
                        posted_dt = datetime.fromisoformat(posted_at)
                        if (now - posted_dt) >= timedelta(days=30):
                            await upstash_del(key)
                            await upstash_srem("active_signals", symbol)
                            print(f"[{datetime.now()}] 🧹 Auto-cleaned {symbol} after 30 days")
                    except Exception as e:
                        print(f"[{datetime.now()}] ❌ Auto-clean parse error for {symbol}: {e}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Cleanup loop error: {e}")
        await asyncio.sleep(poll_interval)

# -----------------------------
# /signal command — now triggers a short manual scan of in-memory state
# -----------------------------
@tg_client.on(events.NewMessage(pattern="/signal"))
async def manual_trigger(event):
    print(f"[{datetime.now()}] /signal received from {event.sender_id}")
    if event.sender_id != ADMIN_ID:
        await event.reply("❌ You are not authorized.")
        return

    await event.reply("⏳ Manual scan: evaluating in-memory trade windows...")
    # Evaluate top candidates in symbol_state and post if they meet conditions
    try:
        candidates = []
        for sym, st in symbol_state.items():
            trades = st.get("trades", [])
            if not trades:
                continue
            volume_now = sum(t['price'] * t['qty'] for t in trades)
            price_now = (sum(t['price'] for t in trades) / len(trades)) if trades else 0
            prev_volume = st.get("last_volume", 1.0)
            prev_price = st.get("last_avg_price", price_now)
            vol_spike = volume_now / (prev_volume + 1e-9)
            price_change = ((price_now - prev_price) / (prev_price + 1e-9)) * 100
            if vol_spike >= VOLUME_SPIKE_THRESHOLD and price_change >= PRICE_CHANGE_THRESHOLD:
                candidates.append((sym, price_now, vol_spike, price_change))
        # sort and post top 3
        candidates.sort(key=lambda x: x[2], reverse=True)
        if not candidates:
            await event.reply("❌ No candidates found in-memory.")
        else:
            top = candidates[:3]
            for sym, p, vs, pc in top:
                await event.reply(f"Posting manual signal for {sym} (vol_spike={vs:.2f}, price_chg={pc:.2f}%)")
                asyncio.create_task(post_signal(sym, p))
            await event.reply("✅ Manual scan completed.")
    except Exception as e:
        await event.reply(f"❌ Manual scan error: {e}")

# -----------------------------
# MAIN STARTUP
# -----------------------------
async def main():
    # start telegram client
    await tg_client.start(bot_token=BOT_TOKEN)
    print(f"[{datetime.now()}] ✅ Telegram client started")

    # create Async Binance client
    client_ws = await AsyncClient.create(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    print(f"[{datetime.now()}] ✅ Binance AsyncClient created")

    # spawn monitor batches (monitor_trades_ws will spawn batch tasks)
    asyncio.create_task(monitor_trades_ws(client_ws, batch_size=20))
    asyncio.create_task(tp_watcher_loop(poll_interval=60))
    asyncio.create_task(cleanup_old_signals_loop(poll_interval=3600))

    print(f"[{datetime.now()}] 🟢 Bot fully started — listening for pumps")
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
