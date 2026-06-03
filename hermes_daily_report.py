#!/usr/bin/env python3
"""
hermes_daily_report.py
Hermes Daily Signal Report - sends yesterday's performance to Telegram
Run by Hermes cron every day at 08:00 Helsinki time
"""
import json
import os
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# --- CONFIG ---
SIGNAL_LOG_FILE = "signals_log.json"
CAPITAL_PER_TRADE = 1000.0
HERMES_BOT_TOKEN = os.getenv("HERMESBOT", "")
HERMES_CHAT_ID = os.getenv("HERMES_CHAT_ID", "")

def send_telegram(message: str):
    if not HERMES_BOT_TOKEN or not HERMES_CHAT_ID:
        print(message)
        return
    try:
        url = f"https://api.telegram.org/bot{HERMES_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": HERMES_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")

def get_pnl(s):
    """Safely extract pnl_usd from a signal, handling None values."""
    val = s.get("pnl_usd")
    return 0.0 if val is None else float(val)

def generate_daily_report():
    """Read signals_log.json and compile yesterday's performance report."""
    if not os.path.exists(SIGNAL_LOG_FILE):
        print(f"[INFO] No signal log found at {SIGNAL_LOG_FILE}")
        return
    with open(SIGNAL_LOG_FILE, "r") as f:
        signals = json.load(f)
    # Get yesterday's date
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    # Filter signals closed yesterday
    yesterdays_signals = [
        s for s in signals
        if s.get("closed_at", "")[:10] == yesterday and s.get("status") in ("WIN", "LOSS")
    ]
    if not yesterdays_signals:
        # If no closed signals yesterday, check if any still open
        open_signals = [s for s in signals if s.get("status") == "OPEN"]
        msg = (
            f"<b>Hermes Daily Report</b>\n"
            f"Date: {yesterday}\n\n"
            f"No signals closed yesterday.\n"
            f"Open signals: {len(open_signals)}"
        )
        send_telegram(msg)
        print(f"[INFO] {yesterday}: No signals closed. {len(open_signals)} open.")
        return
    # Calculate stats
    wins = [s for s in yesterdays_signals if s.get("status") == "WIN"]
    losses = [s for s in yesterdays_signals if s.get("status") == "LOSS"]
    total = len(yesterdays_signals)
    win_rate = round((len(wins) / total) * 100, 1)
    total_pnl = sum(get_pnl(s) for s in yesterdays_signals)
    # Exchange breakdown
    exchanges = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for s in yesterdays_signals:
        exc = s.get("exchange", "Unknown")
        pnl = get_pnl(s)
        if s.get("status") == "WIN":
            exchanges[exc]["wins"] += 1
        else:
            exchanges[exc]["losses"] += 1
        exchanges[exc]["pnl"] += pnl
    # Build message
    pnl_emoji = "" if total_pnl >= 0 else ""
    pnl_str = f"+${total_pnl}" if total_pnl >= 0 else f"-${abs(total_pnl)}"
    msg = (
        f"{pnl_emoji} <b>Hermes Daily Report</b>\n"
        f"Date: <b>{yesterday}</b>\n\n"
        f"Total Signals: <b>{total}</b>\n"
        f"Wins: <b>{len(wins)}</b> | Losses: <b>{len(losses)}</b>\n"
        f"Win Rate: <b>{win_rate}%</b>\n"
        f"Net P&L: <b>{pnl_str}</b>\n\n"
    )
    # Add per-exchange stats
    msg += "<b>Exchange Breakdown:</b>\n"
    for exc, stats in sorted(exchanges.items()):
        exc_pnl = round(stats["pnl"], 2)
        exc_pnl_str = f"+${exc_pnl}" if exc_pnl >= 0 else f"-${abs(exc_pnl)}"
        msg += f"{exc}: {stats['wins']}W / {stats['losses']}L | P&L: {exc_pnl_str}\n"
    send_telegram(msg)
    print(f"[INFO] Daily report sent for {yesterday}")

if __name__ == "__main__":
    generate_daily_report()
