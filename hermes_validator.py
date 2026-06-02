#!/usr/bin/env python3
"""
hermes_validator.py
Hermes Signal Validator - checks OPEN signals against live price on the correct exchange
Run by Hermes cron every 5 minutes
Supports: Binance, KuCoin
"""
import json
import os
import requests
import time
from datetime import datetime, timezone

# --- CONFIG ---
SIGNAL_LOG_FILE = "signals_log.json"
CAPITAL_PER_TRADE = 1000.0  # USD per signal
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Exchange API endpoints mapping
# Binance uses SUIUSDT format, KuCoin uses SUI-USDT format
EXCHANGE_APIS = {
    "Binance": {
        "url": "https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
        "price_key": "price",
        "symbol_format": "{symbol}"  # e.g., SUIUSDT
    },
    "KuCoin": {
        "url": "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}",
        "price_key": "price",
        "symbol_format": "{symbol}"  # e.g., SUI-USDT
    },
    "Bybit": {
        "url": "https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}",
        "price_key": "lastPrice",
        "symbol_format": "{symbol}"  # e.g., SUIUSDT
    }
}

def get_exchange_price(symbol: str, exchange: str) -> float:
    """Fetch current price from the correct exchange based on signal metadata."""
    if exchange not in EXCHANGE_APIS:
        print(f"[WARN] Unknown exchange: {exchange}. Defaulting to Binance.")
        exchange = "Binance"

    api_info = EXCHANGE_APIS[exchange]
    # Format symbol based on exchange requirements
    formatted_symbol = api_info["symbol_format"].format(symbol=symbol)

    # For KuCoin, the pair in signals_log is like "SUIUSDT" but needs "SUI-USDT"
    if exchange == "KuCoin" and "/" not in symbol and "-" not in symbol:
        # Convert SUIUSDT -> SUI-USDT
        # Try to detect quote currency
        for quote in ["USDT", "BTC", "ETH", "USDC"]:
            if symbol.endswith(quote):
                base = symbol[:-len(quote)]
                formatted_symbol = f"{base}-{quote}"
                break

    for attempt in range(3):
        try:
            url = api_info["url"].format(symbol=formatted_symbol)
            headers = {}
            if exchange == "KuCoin":
                headers["Content-Type"] = "application/json"
            r = requests.get(url, headers=headers, timeout=8)
            r.raise_for_status()
            data = r.json()

            # Handle nested response structures
            if exchange == "KuCoin" and "data" in data:
                data = data["data"]
            elif exchange == "Bybit" and "data" in data and "list" in data["data"]:
                data = data["data"]["list"][0] if data["data"]["list"] else {}

            price = float(data.get(api_info["price_key"], 0))
            if price > 0:
                return price
        except Exception as e:
            print(f"[ERROR] {exchange} price fetch attempt {attempt+1}/3 failed for {symbol}: {e}")
            if attempt < 2:
                time.sleep(1)

    print(f"[ERROR] All attempts failed for {symbol} on {exchange}")
    return None

def send_telegram(message: str):
    """Send message to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")

def validate_signals():
    """Main validator - reads signals_log.json, checks OPEN signals against correct exchange, updates results."""
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
        exchange = sig.get("exchange", "Binance")  # Default to Binance for backward compatibility

        # Support tp1/tp2/tp3 or single tp
        tp = float(
            sig.get("tp1") or
            sig.get("tp") or
            (sig.get("tp", [None])[0] if isinstance(sig.get("tp"), list) else None) or
            0
        )

        if not symbol or not entry or not tp or not sl:
            continue

        current_price = get_exchange_price(symbol, exchange)
        if current_price is None:
            continue

        result = None
        outcome = None

        # Validate LONG
        if direction == "LONG":
            if current_price >= tp:
                result = "WIN"
                outcome = "TP HIT"
            elif current_price <= sl:
                result = "LOSS"
                outcome = "SL HIT"
        # Validate SHORT
        elif direction == "SHORT":
            if current_price <= tp:
                result = "WIN"
                outcome = "TP HIT"
            elif current_price >= sl:
                result = "LOSS"
                outcome = "SL HIT"

        if result:
            # Calculate PnL
            if direction == "LONG":
                pnl_pct = (current_price - entry) / entry * 100
            else:
                pnl_pct = (entry - current_price) / entry * 100
            pnl_usd = round(CAPITAL_PER_TRADE * pnl_pct / 100, 2)
            pnl_pct = round(pnl_pct, 2)

            # Update signal record
            sig["status"] = result
            sig["result"] = outcome
            sig["exit_price"] = current_price
            sig["exit_time"] = now
            sig["pnl_usd"] = pnl_usd
            sig["pnl_pct"] = pnl_pct
            sig["closed_at"] = now
            updated = True

            # Send Telegram alert
            if result == "WIN":
                emoji = "✅"
                pnl_str = f"+${pnl_usd} (+{pnl_pct}%)"
            else:
                emoji = "❌"
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
            # Log open signals for debugging
            print(f"[OPEN] {exchange} | {symbol} {direction} | Entry: {entry} | SL: {sl} | TP: {tp} | Current: {current_price}")

    if updated:
        with open(SIGNAL_LOG_FILE, "w") as f:
            json.dump(signals, f, indent=2)
        print(f"[INFO] signals_log.json updated.")
    else:
        print(f"[INFO] No signals closed this run.")

if __name__ == "__main__":
    validate_signals()
