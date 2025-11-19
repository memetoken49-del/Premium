#!/usr/bin/env python3
# premium.py - WebSocket pre-pump scanner (Full, fixed)
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
# UPSTASH HELPERS (synchronous HTTP)
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
STABLECOINS = ["USDT", "BUSD", "USDC", "DAI", "TUSD"]
MIN_TRADE_USD = 500.0
VOLUME_SPIKE_THRESHOLD = 1.5
PRICE_CHANGE_THRESHOLD = 0.5  # percent
TRADE_WINDOW_SIZE = 30  # number of recent trades to aggregate

symbol_state = {}  # in-memory short-term state: symbol -> {trades deque, last_avg_price, last_volume}

def is_stable(symbol: str) -> bool:
    """Detect stablecoin pairs (symbol like BTCUSDT)."""
    for sc in STABLECOINS:
        if symbol.endswith(sc):
            return True
    return False

def calculate_buy_sell_zones(price: float):
    percentages = [0.05, 0.12, 0.20, 0.35, 0.55, 0.85, 1.00]
    sell_zones = [round(price*(1+x), 6) for x in percentages]
    buy_zone_1 = round(price*0.98, 6)
    buy_zone_2 = round(price*0.995 * 1.015, 6)
    return buy_zone_1, buy_zone_2, sell_zones

# -----------------------------
# SIGNAL POSTING (telegram + upstash)
# -----------------------------
async def post_signal(symbol_short: str, price: float):
    """Post to Telegram and store signal in Upstash."""
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

    # save/append
    existing = upstash_get(key)
    if existing:
        try:
            if isinstance(existing, str):
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
        upstash_set(key, payload)
        upstash_sadd_setname("active_signals", symbol)

    # also cache last_price for TP watcher
    upstash_set(f"last_price:{symbol}", {"price": price, "updated_at": now_iso})
    print(f"[{datetime.now()}] ✅ Posted signal {symbol} at {price}")

# -----------------------------
# WEBSOCKET MONITOR (robust)
# -----------------------------
async def monitor_trades_loop(client_ws: AsyncClient):
    """
    Create multiplex trade streams for all USDT pairs and process messages using recv().
    This function auto-reconnects on exceptions.
    """
    print(f"[{datetime.now()}] 🔌 Starting WebSocket monitor loop")
    bsm = BinanceSocketManager(client_ws)

    # Build stream list once; if it fails, retry gracefully.
    while True:
        try:
            info = await client_ws.get_all_tickers()  # single REST call on startup
            usdt_symbols = [t['symbol'] for t in info if t['symbol'].endswith("USDT") and not is_stable(t['symbol'])]
            if not usdt_symbols:
                print(f"[{datetime.now()}] ⚠️ No USDT symbols found; retrying in 10s")
                await asyncio.sleep(10)
                continue

            streams = [f"{s.lower()}@trade" for s in usdt_symbols]
            print(f"[{datetime.now()}] 🟢 Subscribing to {len(streams)} trade streams (multiplex)")

            # multiplex socket (ReconnectingWebsocket-like object) - use recv() to read messages
            async with bsm.multiplex_socket(streams) as ms:
                print(f"[{datetime.now()}] 🟢 WebSocket multiplex connected")
                while True:
                    try:
                        msg = await ms.recv()  # <- correct pattern (no async for)
                        if not msg:
                            await asyncio.sleep(0.01)
                            continue

                        # multiplex returns {'stream': 'btcusdt@trade', 'data': {...}}
                        data = msg.get("data") or msg.get("result") or msg
                        if not data:
                            continue

                        # For trade stream, data contains 's','p','q'
                        symbol_full = data.get("s")
                        if not symbol_full:
                            continue
                        if is_stable(symbol_full):
                            continue

                        price = float(data.get("p", 0))
                        qty = float(data.get("q", 0))
                        trade_value = price * qty

                        # quick filter: ignore tiny trades
                        if trade_value < MIN_TRADE_USD:
                            continue

                        # short symbol like BTC if symbol_full is BTCUSDT
                        symbol_short = symbol_full.replace("USDT", "")

                        # maintain sliding window
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

                        # aggregated metrics
                        volume_now = sum(t['price'] * t['qty'] for t in trades)
                        price_now = (sum(t['price'] for t in trades) / len(trades)) if trades else price

                        # compute spike vs previous short period
                        prev_volume = state.get("last_volume", 1.0)
                        prev_price = state.get("last_avg_price", price_now)

                        volume_spike = volume_now / (prev_volume + 1e-9)
                        price_change = ((price_now - prev_price) / (prev_price + 1e-9)) * 100

                        # update last values for next comparison
                        state["last_volume"] = volume_now
                        state["last_avg_price"] = price_now

                        # Debug occasional:
                        # print(f"{symbol_short} vol_spike={volume_spike:.2f} price_chg={price_change:.3f}% tv={trade_value:.2f}")

                        # Trigger pre-pump detection
                        if volume_spike >= VOLUME_SPIKE_THRESHOLD and price_change >= PRICE_CHANGE_THRESHOLD:
                            # To avoid duplicate posts in a short period, check if a signal exists and is recent
                            existing = upstash_get(f"signal:{symbol_short}")
                            if existing:
                                try:
                                    if isinstance(existing, str):
                                        existing = json.loads(existing)
                                except:
                                    existing = {}
                                posted_at = existing.get("posted_at")
                                if posted_at:
                                    # if posted within last 10 minutes, skip to avoid duplicates
                                    try:
                                        dt = datetime.fromisoformat(posted_at)
                                        if (datetime.now(timezone.utc) - dt).total_seconds() < 600:
                                            continue
                                    except:
                                        pass
                            # Post signal (async)
                            asyncio.create_task(post_signal(symbol_short, price_now))

                        # store last_price for TP watcher (update cache frequently)
                        upstash_set(f"last_price:{symbol_short}", {"price": price, "updated_at": datetime.now(timezone.utc).isoformat()})

                    except Exception as inner_e:
                        print(f"[{datetime.now()}] ⚠️ WebSocket message handling error: {inner_e}")
                        # brief backoff to avoid tight loop on repeated parse errors
                        await asyncio.sleep(0.5)
        except Exception as outer_e:
            print(f"[{datetime.now()}] ❌ WebSocket monitor outer error: {outer_e} — reconnecting in 3s")
            await asyncio.sleep(3)
            continue

# -----------------------------
# TP WATCHER LOOP (uses upstash last_price cache)
# -----------------------------
async def tp_watcher_loop(poll_interval=60):
    print(f"[{datetime.now()}] ⏱ TP watcher started (poll {poll_interval}s)")
    while True:
        try:
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
                lp = upstash_get(f"last_price:{symbol}")
                if lp and isinstance(lp, dict):
                    try:
                        current_price = float(lp.get("price"))
                    except:
                        continue
                else:
                    # fallback: skip if no cached price
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
                    # calculate period
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
                    # Try to reply to the original post (if msg_id exists)
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
                        print(f"[{datetime.now()}] ❌ Failed to send TP msg for {symbol}: {e}")

                    # update/remove targets
                    new_targets = sell_targets[hit_index+1:]
                    if new_targets:
                        data["sell_targets"] = new_targets
                        upstash_set(key, data)
                    else:
                        upstash_srem_setname("active_signals", symbol)
                        upstash_del(key)
        except Exception as e:
            print(f"[{datetime.now()}] ❌ TP watcher error: {e}")
        await asyncio.sleep(poll_interval)

# -----------------------------
# AUTO CLEAN: remove signals older than 30 days
# -----------------------------
async def cleanup_old_signals_loop(poll_interval=3600):
    print(f"[{datetime.now()}] 🧹 Auto-clean loop started (every {poll_interval}s)")
    while True:
        try:
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
                    try:
                        posted_dt = datetime.fromisoformat(posted_at)
                        if (now - posted_dt) >= timedelta(days=30):
                            upstash_del(key)
                            upstash_srem_setname("active_signals", symbol)
                            print(f"[{datetime.now()}] 🧹 Auto-cleaned {symbol} after 30 days")
                    except Exception as e:
                        print(f"[{datetime.now()}] ❌ Auto-clean parse error for {symbol}: {e}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Cleanup loop error: {e}")
        await asyncio.sleep(poll_interval)

# -----------------------------
# Manual /signal command
# -----------------------------
@tg_client.on(events.NewMessage(pattern="/signal"))
async def manual_trigger(event):
    if event.sender_id != ADMIN_ID:
        await event.reply("❌ You are not authorized.")
        return
    await event.reply("⏳ Manual scan acknowledged — monitoring live WebSocket feed.")
    # Optionally, we could run a short manual aggregation pass over symbol_state and post top candidates.
    # For safety, we only notify admin here.
    await event.reply("✅ Manual trigger done.")

# -----------------------------
# STARTUP / MAIN
# -----------------------------
async def main():
    # start Telegram client
    await tg_client.start(bot_token=BOT_TOKEN)
    print(f"[{datetime.now()}] ✅ Telegram client started")

    # create Async Binance client
    client_ws = await AsyncClient.create(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    print(f"[{datetime.now()}] ✅ Binance AsyncClient created")

    # spawn background tasks
    asyncio.create_task(monitor_trades_loop(client_ws))
    asyncio.create_task(tp_watcher_loop(poll_interval=60))
    asyncio.create_task(cleanup_old_signals_loop(poll_interval=3600))

    print(f"[{datetime.now()}] 🟢 Bot fully started — listening for pumps")
    await tg_client.run_until_disconnected()

# -----------------------------
# ENTRYPOINT
# -----------------------------
if __name__ == "__main__":
    # run Flask and self-ping in daemon threads
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()

    # run async main
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted, exiting...")
