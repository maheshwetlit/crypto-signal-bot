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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
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
    if not os.path.exists(SIGNAL_LOG_FILE):
        send_telegram("No signals_log.json found. Bot may not have fired any signals yet.")
        return
    with open(SIGNAL_LOG_FILE, "r") as f:
        all_signals = json.load(f)

    # Get yesterday's date in Helsinki time (UTC+3)
    helsinki_now = datetime.now(timezone.utc) + timedelta(hours=3)
    yesterday = (helsinki_now - timedelta(days=1)).date()
    today = helsinki_now.date()

    # Filter yesterday's signals
    yesterday_signals = []
    for sig in all_signals:
        try:
            sig_time = sig.get("time") or sig.get("timestamp") or ""
            sig_date = datetime.fromisoformat(sig_time.replace("Z", "+00:00")).date()
            if sig_date == yesterday:
                yesterday_signals.append(sig)
        except Exception:
            continue

    total = len(yesterday_signals)
    if total == 0:
        send_telegram(f"No signals logged for {yesterday}. Check if bot is running.")
        return

    wins = [s for s in yesterday_signals if s.get("status") == "WIN"]
    losses = [s for s in yesterday_signals if s.get("status") == "LOSS"]
    still_open = [s for s in yesterday_signals if s.get("status") == "OPEN"]

    win_count = len(wins)
    loss_count = len(losses)
    open_count = len(still_open)
    closed = win_count + loss_count

    win_rate = round(win_count / closed * 100, 1) if closed > 0 else 0.0
    total_pnl = sum(get_pnl(s) for s in yesterday_signals if s.get("status") in ["WIN", "LOSS"])
    total_pnl = round(total_pnl, 2)

    # Profit factor
    gross_win = sum(get_pnl(s) for s in wins if get_pnl(s) > 0)
    gross_loss = abs(sum(get_pnl(s) for s in losses if get_pnl(s) < 0))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else 0.0

    # Best / Worst pair
    pair_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for s in yesterday_signals:
        pair = s.get("pair", "UNKNOWN")
        status = s.get("status", "OPEN")
        pnl = get_pnl(s)
        if status == "WIN":
            pair_stats[pair]["wins"] += 1
        elif status == "LOSS":
            pair_stats[pair]["losses"] += 1
        pair_stats[pair]["pnl"] += pnl

    best_pair = max(pair_stats, key=lambda p: pair_stats[p]["pnl"]) if pair_stats else "N/A"
    worst_pair = min(pair_stats, key=lambda p: pair_stats[p]["pnl"]) if pair_stats else "N/A"

    # Best signal type
    type_wins = defaultdict(int)
    type_total = defaultdict(int)
    for s in yesterday_signals:
        stype = s.get("style") or s.get("type") or s.get("signal_type") or "UNKNOWN"
        type_total[stype] += 1
        if s.get("status") == "WIN":
            type_wins[stype] += 1
    best_type = max(type_wins, key=type_wins.get) if type_wins else "N/A"

    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
    pnl_sign = "+" if total_pnl >= 0 else ""

    report = (
        f"📊 <b>DAILY SIGNAL REPORT</b>\n"
        f"📅 {yesterday} (Helsinki time)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Total Signals: <b>{total}</b>\n"
        f"✅ Wins: <b>{win_count}</b> ❌ Losses: <b>{loss_count}</b> ⏳ Open: <b>{open_count}</b>\n"
        f"🎯 Win Rate: <b>{win_rate}%</b> ({closed} closed)\n"
        f"{pnl_emoji} Net P&L: <b>{pnl_sign}${total_pnl}</b> (@$1,000/signal)\n"
        f"💹 Profit Factor: <b>{profit_factor}</b>\n"
        f"🏆 Best Pair: <b>{best_pair}</b> (+${round(pair_stats[best_pair]['pnl'], 2) if best_pair != 'N/A' else 0})\n"
        f"💧 Worst Pair: <b>{worst_pair}</b> (${round(pair_stats[worst_pair]['pnl'], 2) if worst_pair != 'N/A' else 0})\n"
        f"⭐ Best Signal Type: <b>{best_type}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 Report generated: {today} 08:00 Helsinki"
    )
    send_telegram(report)
    print(report)

if __name__ == "__main__":
    generate_daily_report()
