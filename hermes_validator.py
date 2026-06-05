#!/usr/bin/env python3
"""
hermes_validator.py
Hermes Signal Validator - checks OPEN signals against live price on the correct exchange
Run by Hermes cron every 5 minutes
Supports: Binance, KuCoin, Bybit
"""
import json
import os
import requests
import time
from datetime import datetime, timezone

# --- CONFIG ---
SIGNAL_LOG_FILE = "signals_log.json"
CAPITAL_PER_TRADE = 1000.0  # USD per signal
HERMES_BOT_TOKEN = os.getenv("HERMESBOT", "")
HERMES_CHAT_ID = os.getenv("HERMES_CHAT_ID", "")

# Exchange API endpoints mapping
EXCHANGE_APIS = {
    "Binance": {
        "url": "https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
        "price_key": "price",
    },
    "KuCoin": {
        "url": "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}",
        "price_key": "price",
    },
    "Bybit": {
        "url": "https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}",
        "price_key": "lastPrice",
    }
}


def format_symbol(pair: str, exchange: str) -> str:
    """
    Convert pair symbol to exchange-specific format.
    Data stores pairs as 'BNB/USDT' or 'SUIUSDT'.
    - KuCoin expects: BNB-USDT (dash-separated)
    - Binance expects: BNBUSDT (no separator)
    - Bybit expects: BNBUSDT (no separator)
    """
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
    """Fetch current price from the correct exchange based on signal metadata."""
    if exchange not in EXCHANGE_APIS:
        print(f"[WARN] Unknown exchange: {exchange}. Defaulting to Binance.")
        exchange = "Binance"
    api_info = EXCHANGE_APIS[exchange]
    formatted_symbol = format_symbol(symbol, exchange)
    for attempt in range(3):
        try:
            url = api_info["url"].format(symbol=formatted_symbol)
            r = requests.get(url, timeout=8)
            r.raise_for_status()
            data = r.json()
            if exchange == "KuCoin" and "data" in data:
                data = data["data"]
            elif exchange == "Bybit" and "result" in data:
                tickers = data["result"].get("list", [])
                if tickers:
                    data = tickers[0]
                else:
                    data = {}
            price = float(data.get(api_info["price_key"], 0))
            if price > 0:
                return price
            print(f"[WARN] Zero/empty price from {exchange} for {formatted_symbol} (attempt {attempt+1})")
        except Exception as e:
            print(f"[ERROR] {exchange} price fetch attempt {attempt+1}/3 failed for {formatted_symbol}: {e}")
            if attempt < 2:
                time.sleep(1)
    print(f"[ERROR] All attempts failed for {symbol} (formatted: {formatted_symbol}) on {exchange}")
    return None


def send_telegram(message: str):
    """Send message to Telegram via Hermes bot."""
    if not HERMES_BOT_TOKEN or not HERMES_CHAT_ID:
        print(f"[TELEGRAM] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{HERMES_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": HERMES_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")


def get_tp_value(sig: dict) -> float:
    """Extract TP value from signal, supporting tp, tp1, tp2, tp3, or list format."""
    # Try tp1 first (Hermes-style multi-TP)
    if sig.get("tp1") is not None:
        return float(sig["tp1"])
    # Try single tp (could be number, list, or None)
    tp_raw = sig.get("tp")
    if tp_raw is not None:
        if isinstance(tp_raw, list):
            return float(tp_raw[0]) if tp_raw else 0.0
        return float(tp_raw)
    # Try tp2, tp3 as fallback
    if sig.get("tp2") is not None:
        return float(sig["tp2"])
    if sig.get("tp3") is not None:
        return float(sig["tp3"])
    return 0.0


def validate_signals():
    """Main validator - reads signals_log.json, checks OPEN signals, updates results."""
    if not os.path.exists(SIGNAL_LOG_FILE):
        print(f"[INFO] No signal log found at {SIGNAL_LOG_FILE}")
        return

    with open(SIGNAL_LOG_FILE, "r") as f:
        signals = json.load(f)

    updated = False
    now = datetime.now(timezone.utc).isoformat()

    for sig in signals:
        if sig.get("status") != "OPEN":
            continue

        symbol = sig.get("pair", "")
        direction = sig.get("side", sig.get("direction", "")).upper()
        entry = float(sig.get("entry", 0))
        sl = float(sig.get("sl", 0))
        exchange = sig.get("exchange", "Binance")
        tp = get_tp_value(sig)

        if not symbol or not entry or not tp or not sl:
            print(f"[SKIP] Incomplete signal {sig.get('id', '?')}: symbol={symbol} entry={entry} tp={tp} sl={sl}")
            continue

        current_price = get_exchange_price(symbol, exchange)
        if current_price is None:
            continue

        result = None
        outcome = None

        if direction == "LONG":
            if current_price >= tp:
                result = "WIN"
                outcome = "TP HIT"
            elif current_price <= sl:
                result = "LOSS"
                outcome = "SL HIT"
        elif direction == "SHORT":
            if current_price <= tp:
                result = "WIN"
                outcome = "TP HIT"
            elif current_price >= sl:
                result = "LOSS"
                outcome = "SL HIT"

        if result:
            if direction == "LONG":
                pnl_pct = (current_price - entry) / entry * 100
            else:
                pnl_pct = (entry - current_price) / entry * 100

            pnl_usd = round(CAPITAL_PER_TRADE * pnl_pct / 100, 2)
            pnl_pct = round(pnl_pct, 2)

            sig["status"] = result
            sig["result"] = outcome
            sig["exit_price"] = current_price
            sig["exit_time"] = now
            sig["pnl_usd"] = pnl_usd
            sig["pnl_pct"] = pnl_pct
            updated = True

            if result == "WIN":
                emoji = "\u2705"
                pnl_str = f"+${pnl_usd} (+{pnl_pct}%)"
            else:
                emoji = "\u274c"
                pnl_str = f"-${abs(pnl_usd)} ({pnl_pct}%)"

            msg = (
                f"{emoji} <b>SIGNAL {result}</b>\n"
                f"Exchange: <b>{exchange}</b> | Pair: <b>{symbol}</b> | {direction}\n"
                f"Entry: {entry} | Exit: {current_price}\n"
                f"Result: {outcome}\n"
                f"P&L: {pnl_str} (@${int(CAPITAL_PER_TRADE)} capital)\n"
                f"Time: {now[:16].replace('T', ' ')} UTC"
            )
            send_telegram(msg)
            print(f"[{result}] {exchange} | {symbol} {direction} | {outcome} | PnL: {pnl_str}")
        else:
            print(f"[OPEN] {exchange} | {symbol} {direction} | Entry: {entry} | SL: {sl} | TP: {tp} | Current: {current_price}")

    if updated:
        with open(SIGNAL_LOG_FILE, "w") as f:
            json.dump(signals, f, indent=2)
        print(f"[INFO] signals_log.json updated.")
    else:
        print(f"[INFO] No signals closed this run.")


if __name__ == "__main__":
    validate_signals()
