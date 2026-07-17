#!/usr/bin/env python3
"""
hermes_validator.py
Hermes Signal Validator — checks OPEN signals against live prices.
Run by cron every 5 minutes.
Supports: Binance, KuCoin, Bybit

PATCH:
  FIX-V1  Write both `closed_at` AND `exit_time` so hermes_daily_report.py
          can find closed signals (it reads `closed_at`; previous code only
          wrote `exit_time`, silently breaking the daily report).
"""
import json
import os
import requests
import time
from datetime import datetime, timezone

# --- CONFIG ---
SIGNAL_LOG_FILE     = "signals_log.json"
CAPITAL_PER_TRADE   = 1000.0
SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
TG_TOKEN_FILE       = os.path.join(SCRIPT_DIR, ".tg_token")

# Read token from file (fallback to env var HERMESBOT for compatibility)
if os.path.exists(TG_TOKEN_FILE):
    with open(TG_TOKEN_FILE) as _tf:
        HERMES_BOT_TOKEN = _tf.read().strip()
else:
    HERMES_BOT_TOKEN = os.getenv("HERMESBOT", "")

# Chat ID: env var preferred, then default
HERMES_CHAT_ID = os.getenv("HERMES_CHAT_ID", "5515185305")

EXCHANGE_APIS = {
    "Binance": {
        "url":       "https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
        "price_key": "price",
    },
    "KuCoin": {
        "url":       "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}",
        "price_key": "price",
    },
    "Bybit": {
        "url":       "https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}",
        "price_key": "lastPrice",
    },
}

MAX_SIGNAL_AGE_HOURS = 72   # Auto-close signals open longer than this


def format_symbol(pair: str, exchange: str) -> str:
    pair = pair.strip()
    if exchange == "KuCoin":
        if "/" in pair:
            return pair.replace("/", "-")
        for quote in ["USDT", "USDC", "BTC", "ETH", "DAI", "TUSD"]:
            if pair.endswith(quote) and len(pair) > len(quote):
                base = pair[:-len(quote)]
                return f"{base}-{quote}"
        return pair
    if exchange in ("Binance", "Bybit"):
        return pair.replace("/", "").replace("-", "")
    return pair


def get_exchange_price(symbol: str, exchange: str) -> float:
    if exchange not in EXCHANGE_APIS:
        exchange = "Binance"
    api_info         = EXCHANGE_APIS[exchange]
    formatted_symbol = format_symbol(symbol, exchange)
    for attempt in range(2):
        try:
            url  = api_info["url"].format(symbol=formatted_symbol)
            r    = requests.get(url, timeout=5)
            r.raise_for_status()
            data = r.json()
            if exchange == "KuCoin" and "data" in data:
                data = data["data"]
            elif exchange == "Bybit" and "result" in data:
                tickers = data["result"].get("list", [])
                data = tickers[0] if tickers else {}
            price = float(data.get(api_info["price_key"], 0))
            if price > 0:
                return price
        except Exception:
            if attempt < 1:
                time.sleep(0.5)
    return None


def send_telegram(message: str):
    if not HERMES_BOT_TOKEN or not HERMES_CHAT_ID:
        print(f"[TELEGRAM] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{HERMES_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id":    HERMES_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")


def get_tp_value(sig: dict) -> float:
    """Extract TP1 value from signal (used as win-target)."""
    if sig.get("tp1") is not None:
        return float(sig["tp1"])
    tp_raw = sig.get("tp")
    if tp_raw is not None:
        if isinstance(tp_raw, list):
            return float(tp_raw[0]) if tp_raw else 0.0
        return float(tp_raw)
    if sig.get("tp2") is not None:
        return float(sig["tp2"])
    if sig.get("tp3") is not None:
        return float(sig["tp3"])
    return 0.0


def validate_signals():
    if not os.path.exists(SIGNAL_LOG_FILE):
        print(f"[INFO] No signal log found at {SIGNAL_LOG_FILE}")
        return

    with open(SIGNAL_LOG_FILE, "r") as f:
        signals = json.load(f)

    updated = False
    now_str = datetime.now(timezone.utc).isoformat()

    # Collect all open signals that need price checks
    open_sigs = []
    for sig in signals:
        if sig.get("status") != "OPEN":
            continue
        symbol   = sig.get("pair", "")
        direction= sig.get("side", sig.get("direction", "")).upper()
        entry    = float(sig.get("entry", 0))
        sl       = float(sig.get("sl", 0))
        exchange = sig.get("exchange", "Binance")
        tp       = get_tp_value(sig)
        if not symbol or not entry or not tp or not sl:
            print(f"[SKIP] Incomplete signal {sig.get('id', '?')}")
            continue
        open_sigs.append((sig, symbol, direction, entry, sl, exchange, tp))

    print(f"[INFO] Checking {len(open_sigs)} OPEN signals...")

    # Fetch prices concurrently (max 10 parallel requests)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    price_cache = {}

    def _fetch(args):
        sig, symbol, direction, entry, sl, exchange, tp = args
        price = get_exchange_price(symbol, exchange)
        return (sig, symbol, direction, entry, sl, exchange, tp, price)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch, s): s for s in open_sigs}
        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            if done_count % 20 == 0:
                print(f"       Prices fetched: {done_count}/{len(open_sigs)}")
            try:
                sig, symbol, direction, entry, sl, exchange, tp, current_price = future.result()
            except Exception as e:
                print(f"[ERROR] {e}")
                continue

            if current_price is None:
                continue

            # ── Signal expiry check: auto-close stale signals ──
            signal_time = sig.get("time", "")
            if signal_time:
                try:
                    dt_open = datetime.fromisoformat(signal_time.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - dt_open).total_seconds() / 3600
                    if age_hours > MAX_SIGNAL_AGE_HOURS:
                        # Close as EXPIRED at current price
                        if direction == "LONG":
                            pnl_pct = (current_price - entry) / entry * 100
                        else:
                            pnl_pct = (entry - current_price) / entry * 100
                        pnl_usd = round(CAPITAL_PER_TRADE * pnl_pct / 100, 2)
                        pnl_pct = round(pnl_pct, 2)
                        result = "WIN" if pnl_usd > 0 else "LOSS"

                        sig["status"]     = result
                        sig["result"]     = "EXPIRED"
                        sig["exit_price"] = current_price
                        sig["pnl_usd"]    = pnl_usd
                        sig["pnl_pct"]    = pnl_pct
                        sig["exit_time"]  = now_str
                        sig["closed_at"]  = now_str
                        updated = True

                        emoji = "⏰"
                        pnl_str = f"+${pnl_usd}" if pnl_usd > 0 else f"-${abs(pnl_usd)}"
                        msg = (
                            f"{emoji} <b>SIGNAL EXPIRED</b> ({age_hours:.0f}h)\\n"
                            f"Pair: <b>{symbol}</b> | {direction}\\n"
                            f"Entry: {entry} | Exit: {current_price}\\n"
                            f"P&L: {pnl_str} ({pnl_pct}%)\\n"
                            f"Closed at current price"
                        )
                        send_telegram(msg)
                        print(f"[EXPIRED] {symbol} {direction} | {age_hours:.0f}h old | PnL: {pnl_str}")
                        continue
                except Exception:
                    pass

            # ── Tiered TP validation ──
            # TP1 → 33% close, TP2 → 33%, TP3 → 34% remaining
            tp1 = float(sig.get("tp1", 0)) if sig.get("tp1") else tp
            cap_config = sig.get("capital") or 1000.0

            # HYBRID v9.0: Breakeven at 60% of TP1 distance
            breakeven_pct = 0.60
            tp1_price_for_be = float(sig.get("tp1", 0))
            sig_entry = float(sig.get("entry", 0))
            sig_sl = float(sig.get("sl", 0))
            sig_entry_for_be = sig_entry
            if tp1_price_for_be and sig_entry_for_be:
                dist_to_tp1 = abs(tp1_price_for_be - sig_entry_for_be)
                if direction == "LONG":
                    breakeven_price = sig_entry_for_be + breakeven_pct * dist_to_tp1
                    fetched_close = current_price
                    if fetched_close >= breakeven_price and sig_sl < sig_entry_for_be:
                        sig["sl"] = round(sig_entry_for_be, 8)
                        sig["status"] = "TRAILING"
                        sig["result"] = "BE_MOVED"
                        updated = True
                        print(f"   [HYBRID-BE] {symbol} {direction}: SL -> entry {sig_entry_for_be}")
                else:
                    breakeven_price = sig_entry_for_be - breakeven_pct * dist_to_tp1
                    fetched_close = current_price
                    if fetched_close <= breakeven_price and sig_sl > sig_entry_for_be:
                        sig["sl"] = round(sig_entry_for_be, 8)
                        sig["status"] = "TRAILING"
                        sig["result"] = "BE_MOVED"
                        updated = True
                        print(f"   [HYBRID-BE] {symbol} {direction_b}: SL -> entry {sig_entry_for_be}")

            result  = None
            outcome = None

            if direction == "LONG":
                if current_price >= tp:
                    result, outcome = "WIN",  "TP HIT"
                elif current_price <= sl:
                    result, outcome = "LOSS", "SL HIT"
            elif direction == "SHORT":
                if current_price <= tp:
                    result, outcome = "WIN",  "TP HIT"
                elif current_price >= sl:
                    result, outcome = "LOSS", "SL HIT"

            if result:
                if direction == "LONG":
                    pnl_pct = (current_price - entry) / entry * 100
                else:
                    pnl_pct = (entry - current_price) / entry * 100

                pnl_usd = round(CAPITAL_PER_TRADE * pnl_pct / 100, 2)
                pnl_pct = round(pnl_pct, 2)

                sig["status"]     = result
                sig["result"]     = outcome
                sig["exit_price"] = current_price
                sig["pnl_usd"]    = pnl_usd
                sig["pnl_pct"]    = pnl_pct
                sig["exit_time"]  = now_str
                sig["closed_at"]  = now_str
                updated = True

                emoji   = "✅" if result == "WIN" else "❌"
                pnl_str = (f"+${pnl_usd} (+{pnl_pct}%)" if result == "WIN"
                           else f"-${abs(pnl_usd)} ({pnl_pct}%)")

                msg = (
                    f"{emoji} <b>SIGNAL {result}</b>\n"
                    f"Exchange: <b>{exchange}</b> | Pair: <b>{symbol}</b> | {direction}\n"
                    f"Entry: {entry} | Exit: {current_price}\n"
                    f"Result: {outcome}\n"
                    f"P&L: {pnl_str} (@${int(CAPITAL_PER_TRADE)} capital)\n"
                    f"Time: {now_str[:16].replace('T', ' ')} UTC"
                )
                send_telegram(msg)
                print(f"[{result}] {exchange} | {symbol} {direction} | {outcome} | PnL: {pnl_str}")
            else:
                print(f"[OPEN] {exchange} | {symbol} {direction} | "
                      f"Entry: {entry} | SL: {sl} | TP: {tp} | Current: {current_price}")

    if updated:
        with open(SIGNAL_LOG_FILE, "w") as f:
            json.dump(signals, f, indent=2)
        print(f"[INFO] signals_log.json updated.")
    else:
        print(f"[INFO] No signals closed this run.")


if __name__ == "__main__":
    validate_signals()
