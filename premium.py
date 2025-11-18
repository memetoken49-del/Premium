#!/usr/bin/env python3
import os
import asyncio
import requests
from telethon import TelegramClient, events
from flask import Flask
import threading
import json
from datetime import datetime, timezone, timedelta
import time

# -----------------------------
# ENVIRONMENT VARIABLES (set in env if you want)
# -----------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

# Optional: your Binance API key (not required for public market endpoints)
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", None)

# Upstash REST API details
UPSTASH_REST_URL = os.getenv("UPSTASH_REST_URL", "https://eager-shrew-32373.upstash.io")
UPSTASH_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_TOKEN", "AX51AAIncDJiMzI3OGMwNWM4OTQ0ZTU0YWU5NzdjODk3NDk5Y2NmZnAyMzIzNzM")

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
    return "✅ Pre-Pump Scanner Bot Running (Binance API mode)"

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
        time.sleep(240)  # every 4 minutes

# -----------------------------
# HELPERS: Upstash REST helpers (simple wrappers)
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
# UTILITIES: Binance helpers
# -----------------------------
BINANCE_BASE = "https://api.binance.com"
COMMON_HEADERS = {}
if BINANCE_API_KEY:
    COMMON_HEADERS["X-MBX-APIKEY"] = BINANCE_API_KEY

def binance_get(path, params=None, timeout=10):
    url = BINANCE_BASE + path
    try:
        r = requests.get(url, params=params, headers=COMMON_HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Binance GET {path} error: {e}")
        return None

# -----------------------------
# HELPER FUNCTIONS (coin logic)
# -----------------------------
def is_stable(symbol):
    return any(s in symbol.upper() for s in ["USDT","BUSD","USDC","DAI","TUSD"])

def calculate_buy_sell_zones(price):
    """
    Returns buy_zone_1, buy_zone_2, sell_zones(list)
    """
    percentages = [0.05, 0.12, 0.20, 0.35, 0.55, 0.85, 1.00]
    sell_zones = [round(price*(1+x), 6) for x in percentages]
    buy_zone_1 = round(price*0.98, 6)
    buy_zone_2 = round(price*0.995 * 1.015, 6)  # 1.5% allowance added
    return buy_zone_1, buy_zone_2, sell_zones

# -----------------------------
# FETCH COINS (Binance-only)
# -----------------------------
async def fetch_coins():
    """
    Returns a list of coins that have a Binance USDT market.
    Uses /api/v3/ticker/24hr to avoid individual requests.
    Each entry: { 'symbol': base, 'full_symbol': SYMBOL (e.g. RAYUSDT), 'current_price': float, 'quoteVolume': float, 'volume': float }
    """
    binance_coins = []
    try:
        data = binance_get("/api/v3/ticker/24hr")
        if not data:
            return binance_coins

        for t in data:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]  # remove 'USDT'
            try:
                price = float(t.get("lastPrice", t.get("price", 0) or 0))
            except:
                price = 0.0
            # quoteVolume is volume in quote asset (USDT) — a good proxy for USD volume
            try:
                qvol = float(t.get("quoteVolume", 0.0))
            except:
                qvol = 0.0
            try:
                base_vol = float(t.get("volume", 0.0))
            except:
                base_vol = 0.0

            binance_coins.append({
                "symbol": base,
                "full_symbol": sym,
                "current_price": price,
                "quoteVolume": qvol,
                "baseVolume": base_vol
            })

    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error fetching Binance tickers: {e}")

    return binance_coins

# -----------------------------
# SIGNAL POSTING (stores in Upstash and in an 'active_signals' set)
# -----------------------------
async def post_signal(c):
    """
    Post message to CHANNEL_ID and store metadata in Upstash.
    Supports multiple reposts and tracks original timestamps.
    """
    symbol = c["symbol"].upper()
    full_symbol = c["full_symbol"]
    key = f"signal:{symbol}"

    # Calculate zones
    price = float(c["current_price"])
    buy1, buy2, sells = calculate_buy_sell_zones(price)

    msg = f"🚀 Binance\n#{symbol}/USDT\n"
    msg += f"Buy zone {buy1}-{buy2}\n"
    msg += "Sell zone " + " - ".join([str(sz) for sz in sells]) + "\n"
    msg += "Margin 3x"

    # send message
    try:
        sent = await client.send_message(CHANNEL_ID, msg)
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Failed to send message to channel: {e}")
        return

    msg_id = getattr(sent, "id", None)
    now_iso = datetime.now(timezone.utc).isoformat()

    existing = upstash_get(key)
    if existing:
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except:
                existing = {}

        # Append new message ID with timestamp
        msg_ids = existing.get("msg_ids", [])
        msg_ids.append({"msg_id": msg_id, "posted_at": now_iso})

        # Update payload
        payload = existing
        payload["msg_ids"] = msg_ids
        payload["buy_price"] = price
        payload["sell_targets"] = sells
        payload["posted_at"] = now_iso
        upstash_set(key, payload)
        print(f"[{datetime.now()}] 🔄 Appended new msg_id for {symbol}: {msg_id}")
    else:
        # First time posting
        payload = {
            "msg_ids": [{"msg_id": msg_id, "posted_at": now_iso}],
            "symbol": symbol,
            "full_symbol": full_symbol,
            "coin_id": full_symbol,  # we use full_symbol as coin_id analog
            "buy_price": price,
            "sell_targets": sells,
            "posted_at": now_iso,
            "posted_by": "bot"
        }
        upstash_set(key, payload)
        upstash_sadd_setname("active_signals", symbol)
        print(f"[{datetime.now()}] ✅ Posted and tracked {symbol} (msg_id={msg_id})")

# -----------------------------
# SCAN AND POST PRE-PUMP COINS (main scanner logic)
# -----------------------------
async def scan_and_post(auto=False):
    coins = await fetch_coins()  # Binance USDT coins only
    candidates = []

    for c in coins:
        symbol = c["symbol"].upper()
        full_symbol = c["full_symbol"]
        if is_stable(symbol):
            continue

        # Skip coins with tiny USDT quote volume (day)
        if float(c.get("quoteVolume", 0.0)) < 100:  # skip extremely low 24h volume
            continue

        # Fetch last 15 minutes klines (1m)
        try:
            params = {"symbol": full_symbol, "interval": "1m", "limit": 15}
            klines = binance_get("/api/v3/klines", params=params)
            if not klines or len(klines) < 3:
                continue

            # klines: [ [openTime, open, high, low, close, volume, closeTime, quoteAssetVolume, ...], ... ]
            prices = [float(k[4]) for k in klines]  # close prices
            quote_volumes = [float(k[7]) for k in klines]  # quote asset volume (USDT)

            price_now = float(c.get("current_price") or prices[-1])
            price_earlier = float(prices[0])
            # protect against zero
            if price_earlier <= 0:
                continue
            change_short = ((price_now - price_earlier) / price_earlier) * 100.0

            volume_now = quote_volumes[-1]  # most recent minute's quote volume
            volume_earlier = quote_volumes[0] + 1e-9
            volume_spike = volume_now / volume_earlier

        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error fetching klines for {full_symbol}: {e}")
            continue

        # -----------------------------
        # Loosened Pre-Pump Filters (tunable)
        # -----------------------------
        min_volume_absolute = 500.0      # must have at least $500 volume in last minute
        volume_spike_threshold = 1.5    # volume must increase 1.5x
        price_change_threshold = 0.5    # price up at least 0.5%

        # skip tiny dead coins with almost no liquidity
        if volume_now < min_volume_absolute:
            continue

        if volume_spike < volume_spike_threshold:
            continue

        if change_short < price_change_threshold:
            continue

        # passed filter
        c["short_term_change"] = change_short
        c["short_term_volume_ratio"] = volume_spike
        candidates.append(c)

    # Sort descending by short-term % change
    candidates.sort(key=lambda x: x.get("short_term_change", 0.0), reverse=True)

    if not candidates:
        msg = "❌ No early pump candidates found."
        suffix = "(auto scan)" if auto else "(manual scan)"
        try:
            await client.send_message(ADMIN_ID, msg + " " + suffix)
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Failed to notify admin: {e}")
        return

    # Only post top candidate (append if already exists)
    coin = candidates[0]
    await post_signal(coin)

# -----------------------------
# TP Watcher (background loop) — runs every 60 seconds
# -----------------------------
async def tp_watcher_loop(poll_interval=60):
    while True:
        try:
            symbols = upstash_smembers("active_signals") or []
            if not symbols:
                await asyncio.sleep(poll_interval)
                continue

            for symbol in list(symbols):
                key = f"signal:{symbol}"
                data = upstash_get(key)
                if not data:
                    upstash_srem_setname("active_signals", symbol)
                    continue

                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except:
                        pass

                full_symbol = data.get("full_symbol") or (symbol + "USDT")
                coin_id = full_symbol
                if not coin_id:
                    continue

                # fetch current price (Binance)
                try:
                    price_resp = binance_get("/api/v3/ticker/price", params={"symbol": full_symbol})
                    if not price_resp:
                        print(f"[{datetime.now()}] ❌ Price response empty for {full_symbol}")
                        continue
                    current_price = float(price_resp.get("price", 0.0))
                except Exception as e:
                    print(f"[{datetime.now()}] ❌ Binance price fetch error for {full_symbol}: {e}")
                    continue

                buy_price = float(data.get("buy_price"))
                sell_targets = data.get("sell_targets", [])
                if not sell_targets:
                    upstash_srem_setname("active_signals", symbol)
                    upstash_del(key)
                    continue

                # Check targets in ascending order
                hit_index = None
                for idx, t in enumerate(sell_targets):
                    t_float = float(t)
                    if current_price >= t_float:
                        hit_index = idx
                        break

                if hit_index is not None:
                    target_price = float(sell_targets[hit_index])
                    profit_pct = ((target_price - buy_price) / buy_price) * 100.0
                    leverage_profit = profit_pct * 3.0

                    # Period calculation
                    try:
                        posted_at = datetime.fromisoformat(data.get("posted_at"))
                        delta = datetime.now(timezone.utc) - posted_at
                        hours, rem = divmod(int(delta.total_seconds()), 3600)
                        minutes, _ = divmod(rem, 60)
                        period_str = f"{hours} Hours {minutes} Minutes"
                    except:
                        period_str = "N/A"

                    msg = f"Binance\n#{symbol}/USDT Take-Profit target {hit_index+1} ✅\n"
                    msg += f"Profit: {leverage_profit:.4f}% 📈\nPeriod: {period_str} ⏰\n"

                    # Find closest message by posted_at
                    messages = data.get("msg_ids", [])
                    original_msg_id = None
                    if messages:
                        try:
                            original_time = datetime.fromisoformat(data.get("posted_at"))
                            closest_msg = min(
                                messages,
                                key=lambda x: abs(datetime.fromisoformat(x["posted_at"]) - original_time)
                            )
                            original_msg_id = closest_msg["msg_id"]
                        except:
                            original_msg_id = messages[-1]["msg_id"]

                    # send TP message
                    if original_msg_id:
                        try:
                            await client.send_message(CHANNEL_ID, msg, reply_to=original_msg_id)
                        except Exception as e:
                            print(f"[{datetime.now()}] ❌ Failed to reply TP for {symbol} msg_id={original_msg_id}: {e}")

                    # remove hit targets
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
# MONTHLY CLEANUP (runs once a day at midnight UTC)
# -----------------------------
async def monthly_cleanup_loop():
    while True:
        now = datetime.now(timezone.utc)
        # Only run at 00:00 UTC
        if now.hour == 0 and now.minute == 0:
            # Check if today is the last day of the month
            next_day = now + timedelta(days=1)
            if next_day.day == 1:  # tomorrow is the first day of next month
                print(f"[{datetime.now()}] 🧹 Monthly cleanup running...")

                symbols = upstash_smembers("active_signals") or []
                for symbol in symbols:
                    upstash_del(f"signal:{symbol}")
                    upstash_srem_setname("active_signals", symbol)

                print(f"[{datetime.now()}] ✅ Monthly cleanup done for {len(symbols)} signals.")

            # Sleep 61 seconds to avoid double run in same minute
            await asyncio.sleep(61)
        else:
            # Sleep 30 seconds and check again
            await asyncio.sleep(30)

# -----------------------------
# TELEGRAM /signal COMMAND (manual)
# -----------------------------
@client.on(events.NewMessage(pattern="/signal"))
async def manual_trigger(event):
    user_id = event.sender_id
    if user_id != ADMIN_ID:
        await event.reply("❌ You are not authorized.")
        return
    await event.reply("⏳ Manual scan started — checking 1 coin only...")
    await scan_and_post(auto=False)
    await event.reply("✅ Manual scan completed.")

# -----------------------------
# AUTO SCAN LOOP (every 10 minutes)
# -----------------------------
async def auto_scan_loop():
    while True:
        print(f"[{datetime.now()}] 🔍 Auto scan running...")
        await scan_and_post(auto=True)
        await asyncio.sleep(600)  # 10 minutes

# -----------------------------
# MAIN
# -----------------------------
async def main():
    await client.start(bot_token=BOT_TOKEN)
    print("✅ Pre-Pump Scanner Bot is live (Binance mode)")
    # start TP watcher background tasks
    asyncio.create_task(tp_watcher_loop(poll_interval=60))
    asyncio.create_task(auto_scan_loop())
    asyncio.create_task(monthly_cleanup_loop())
    # Keep bot running:
    await client.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    asyncio.run(main())
