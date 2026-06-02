#!/usr/bin/env python3
"""
hermes_validator.py
Hermes Signal Validator - checks OPEN signals against Binance live price
Run by Hermes cron every 5 minutes
"""

import json
import os
import requests
from datetime import datetime, timezone

# --- CONFIG ---
SIGNAL_LOG_FILE = "signals_log.json"
CAPITAL_PER_TRADE = 1000.0  # USD per signal
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def get_binance_price(symbol: str) -> float:
    """Fetch current price from Binance public API."""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        print(f"[ERROR] Binance price fetch failed for {symbol}: {e}")
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

        # Support tp1/tp2/tp3 or single tp
        tp = float(
            sig.get("tp1") or
            sig.get("tp") or
            (sig.get("tp", [None])[0] if isinstance(sig.get("tp"), list) else None) or
            0
        )

        if not symbol or not entry or not tp or not sl:
            continue

        current_price = get_binance_price(symbol)
        if current_price is None:
            continue

        result = None
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
                f"Pair: <b>{symbol}</b> | {direction}\n"
                f"Entry: {entry} | Exit: {current_price}\n"
                f"Result: {outcome}\n"
                f"P&L: {pnl_str} (@${int(CAPITAL_PER_TRADE)} capital)\n"
                f"Time: {now[:16].replace('T', ' ')} UTC"
            )
            send_telegram(msg)
            print(f"[{result}] {symbol} {direction} | {outcome} | PnL: {pnl_str}")

    if updated:
        with open(SIGNAL_LOG_FILE, "w") as f:
            json.dump(signals, f, indent=2)
        print(f"[INFO] signals_log.json updated.")
    else:
        print(f"[INFO] No signals closed this run.")


if __name__ == "__main__":
    validate_signals()
