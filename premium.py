#!/usr/bin/env python3
import os
import asyncio
import requests
from telethon import TelegramClient, events
from flask import Flask
import threading
import json
from datetime import datetime, timezone
import time

# -----------------------------
# ENVIRONMENT VARIABLES (set in env if you want)
# -----------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

# Upstash REST API details
# Defaults are the values you provided; it's safer to set these as env vars in production.
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
        time.sleep(240)  # every 4 minutes

# -----------------------------
# HELPERS: Upstash REST helpers (simple wrappers)
# -----------------------------
UP_HEADERS = {"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"}

def upstash_set(key: str, value) -> dict:
    """
    Stores JSON value at key. Uses POST so value can be JSON body.
    """
    url = f"{UPSTASH_REST_URL}/set/{key}"
    try:
        # Send JSON string as POST body (Upstash appends body as last arg)
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

import requests
from datetime import datetime

async def fetch_coins():
    """
    Returns a list of coins that have a Binance USDT market.
    Optimized: avoids fetching all tickers individually.
    """
    binance_coins = []

    try:
        # Get all tickers from CoinGecko
        response = requests.get(
            "https://api.coingecko.com/api/v3/exchanges/binance/tickers",
            params={"include_exchange_logo": "false"},
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        tickers = data.get("tickers", [])

        coin_ids = set()
        for t in tickers:
            if t.get("target") == "USDT":
                coin_id = t.get("coin_id")
                if coin_id and coin_id not in coin_ids:
                    coin_ids.add(coin_id)
                    # minimal info needed
                    binance_coins.append({
                        "id": coin_id,
                        "symbol": t.get("base"),
                        "current_price": t.get("last")
                    })

    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error fetching Binance USDT coins: {e}")

    return binance_coins

# -----------------------------
# SIGNAL POSTING (stores in Upstash and in an 'active_signals' set)
# -----------------------------
async def post_signal(c):
    """
    Post message to CHANNEL_ID and store metadata in Upstash:
    Key: signal:{SYMBOL}
    Value: JSON with msg_id, symbol, coin_id, buy_price, sell_targets, timestamp, posted_by
    Also adds symbol to Redis set 'active_signals'
    """
    symbol = c["symbol"].upper()  # e.g., QNT
    full_symbol = f"{symbol}/USDT (Binance)"
    key = f"signal:{symbol}"
    # Check if already exist in Upstash (avoid duplicates)
    existing = upstash_get(key)
    if existing:
        # already posted and tracked
        return

    price = c["current_price"]
    buy1, buy2, sells = calculate_buy_sell_zones(price)

    msg = f"🚀 Binance\n#{symbol}/USDT\n"
    msg += f"Buy zone {buy1}-{buy2}\n"
    msg += "Sell zone " + " - ".join([str(sz) for sz in sells]) + "\n"
    msg += "Margin 3x"

    # send message and capture message id
    sent = await client.send_message(CHANNEL_ID, msg)
    msg_id = getattr(sent, "id", None)

    now_iso = datetime.now(timezone.utc).isoformat()

    # store metadata in Upstash
    payload = {
        "msg_id": msg_id,
        "symbol": symbol,
        "full_symbol": full_symbol,
        "coin_id": c.get("id"),            # coingecko id (used for price fetch)
        "buy_price": price,
        "sell_targets": sells,             # list of target prices
        "posted_at": now_iso,
        "posted_by": "bot"
    }
    upstash_set(key, payload)
    # add symbol to active set
    upstash_sadd_setname("active_signals", symbol)
    print(f"[{datetime.now()}] ✅ Posted and tracked {symbol} (msg_id={msg_id})")

# -----------------------------
# -----------------------------
# SCAN AND POST PRE-PUMP COINS (main scanner logic)
# -----------------------------
async def scan_and_post(auto=False):
    coins = await fetch_coins()  # Binance USDT coins only
    candidates = []

    for c in coins:
        symbol = c["symbol"].upper()
        if is_stable(symbol):
            continue  # skip stablecoins

        coin_id = c.get("id")
        if not coin_id:
            continue

        # Fetch 24h price data for this coin
        try:
            history = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": 1, "interval": "hourly"},
                timeout=10
            ).json()

            prices = history.get("prices", [])
            if len(prices) < 2:
                continue

            price_now = float(c.get("current_price"))
            price_24h_ago = float(prices[0][1])
            change_24h = ((price_now - price_24h_ago) / price_24h_ago) * 100

            volume_24h = float(history.get("total_volumes", [[0,0]])[-1][1])
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error fetching 24h data for {symbol}: {e}")
            continue

        # Apply filters
        if volume_24h < 50_000:
            continue
        if change_24h < 5:
            continue

        # add extra info
        c["price_change_percentage_24h"] = change_24h
        c["total_volume"] = volume_24h

        candidates.append(c)

    # Sort by 24h change descending
    candidates.sort(key=lambda x: x["price_change_percentage_24h"], reverse=True)

    if not candidates:
        msg = "❌ No suitable pre-pump candidates found."
        if auto:
            await client.send_message(ADMIN_ID, msg + " (auto scan)")
        else:
            await client.send_message(ADMIN_ID, msg + " (manual scan)")
        return

    # Only post 1 coin
    coin = candidates[0]
    await post_signal(coin)
# -----------------------------
# TP Watcher (background loop) — runs every 60 seconds
# -----------------------------
async def tp_watcher_loop(poll_interval=60):
    """
    This loop checks all active signals stored in Upstash. For each signal it:
      - fetches current price from CoinGecko
      - checks each sell target in ascending order
      - if a target is hit, calculates profit% and multiplies by 3 (margin)
      - replies to the original message with the TP message
      - removes hit target(s) and updates Redis; if no targets left, remove signal entirely
    """
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
                    # remove from active set if data missing
                    upstash_srem_setname("active_signals", symbol)
                    continue

                # data might be stored as JSON string — ensure dict
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except:
                        pass

                coin_id = data.get("coin_id")
                if not coin_id:
                    # no coingecko id: skip
                    continue

                # fetch current price from CoinGecko simple price API
                try:
                    cg = requests.get(
                        "https://api.coingecko.com/api/v3/simple/price",
                        params={"ids": coin_id, "vs_currencies": "usd"},
                        timeout=10
                    ).json()
                    current_price = cg.get(coin_id, {}).get("usd")
                    if current_price is None:
                        continue
                except Exception as e:
                    print(f"[{datetime.now()}] ❌ CoinGecko price fetch error for {coin_id}: {e}")
                    continue

                buy_price = float(data.get("buy_price"))
                sell_targets = data.get("sell_targets", [])
                if not sell_targets:
                    # nothing to do, cleanup
                    upstash_srem_setname("active_signals", symbol)
                    upstash_del(key)
                    continue

                # Check targets in ascending order; when hit, send TP message and remove that target
                hit_index = None
                for idx, t in enumerate(sell_targets):
                    t_float = float(t)
                    # target is hit if current_price >= target
                    if current_price >= t_float:
                        hit_index = idx
                        break

                if hit_index is not None:
                    target_price = float(sell_targets[hit_index])
                    # profit % (unlevered)
                    profit_pct = ((target_price - buy_price) / buy_price) * 100.0
                    # leverage 3x
                    leverage_profit = profit_pct * 3.0
                    # period = now - posted_at
                    try:
                        posted_at = datetime.fromisoformat(data.get("posted_at"))
                        period_delta = datetime.now(timezone.utc) - posted_at
                        # Format period human-friendly
                        hours, remainder = divmod(int(period_delta.total_seconds()), 3600)
                        minutes, seconds = divmod(remainder, 60)
                        period_str = f"{hours} Hours {minutes} Minutes"
                    except Exception:
                        period_str = "N/A"

                    # compose reply message (reply to original message id)
                    msg = "Binance\n"
                    msg += f"#{symbol}/USDT Take-Profit target {hit_index+1} ✅\n"
                    msg += f"Profit: {leverage_profit:.4f}% 📈\n"
                    msg += f"Period: {period_str} ⏰\n"

                    # reply to original message in same channel (threaded reply)
                    original_msg_id = data.get("msg_id")
                    try:
                        await client.send_message(CHANNEL_ID, msg, reply_to=original_msg_id)
                        print(f"[{datetime.now()}] ✅ TP hit for {symbol} target {hit_index+1}: {leverage_profit:.4f}%")
                    except Exception as e:
                        print(f"[{datetime.now()}] ❌ Failed to reply TP for {symbol}: {e}")

                    # remove that target from list and update storage
                    new_targets = sell_targets[hit_index+1:]  # remove up to and including hit target
                    if new_targets:
                        data["sell_targets"] = new_targets
                        upstash_set(key, data)
                    else:
                        # no more targets: remove everything
                        upstash_srem_setname("active_signals", symbol)
                        upstash_del(key)

            # sleep until next poll
        except Exception as e:
            print(f"[{datetime.now()}] ❌ TP watcher error: {e}")

        await asyncio.sleep(poll_interval)

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
    print("✅ Pre-Pump Scanner Bot is live")
    # start TP watcher background task
    asyncio.create_task(tp_watcher_loop(poll_interval=60))
    asyncio.create_task(auto_scan_loop())
    # Only manual scans (you can still call /signal). Keep bot running:
    await client.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    asyncio.run(main())
