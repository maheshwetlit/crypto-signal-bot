#!/usr/bin/env python3
"""
hermes_hourly_report.py
Fetches signals from GitHub, runs the validator, then sends a formatted
summary report to Telegram matching the expected output format.

Usage: python hermes_hourly_report.py [--dry-run] [--skip-prices]
"""
import json
import os
import sys
import time
import base64
import urllib.request
from datetime import datetime, timezone
from collections import defaultdict

import requests

# ── Config ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

GH_TOKEN_FILE = os.path.join(SCRIPT_DIR, ".gh_token")
TG_TOKEN_FILE  = os.path.join(SCRIPT_DIR, ".tg_token")
SIGNAL_LOG_FILE = os.path.join(SCRIPT_DIR, "signals_log.json")
GH_API_URL     = "https://api.github.com/repos/maheshwetlit/crypto-signal-bot/contents/signals_log.json"
CHAT_ID        = "5515185305"
CAPITAL        = 1000.0

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
    },
}


def load_token(path):
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return ""


def gh_fetch_signals():
    token = load_token(GH_TOKEN_FILE)
    if not token:
        print("[ERROR] No GitHub token found")
        sys.exit(1)
    req = urllib.request.Request(GH_API_URL, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github.v3+json"
    })
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read())
    raw = base64.b64decode(data["content"]).decode()
    return json.loads(raw)


def gh_commit_signals(signals):
    token = load_token(GH_TOKEN_FILE)
    content = json.dumps(signals, indent=2)
    encoded = base64.b64encode(content.encode()).decode()
    req = urllib.request.Request(GH_API_URL, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github.v3+json"
    })
    resp = urllib.request.urlopen(req, timeout=15)
    sha = json.loads(resp.read())["sha"]
    payload = json.dumps({
        "message": "Signal update " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "content": encoded,
        "sha": sha
    }).encode()
    req2 = urllib.request.Request(GH_API_URL, data=payload, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }, method="PUT")
    resp2 = urllib.request.urlopen(req2, timeout=15)
    return resp2.status == 200


def format_symbol(pair, exchange):
    pair = pair.strip()
    if exchange == "KuCoin":
        if "/" in pair:
            return pair.replace("/", "-")
        for quote in ["USDT", "USDC", "BTC", "ETH", "DAI", "TUSD"]:
            if pair.endswith(quote) and len(pair) > len(quote):
                return pair[:-len(quote)] + "-" + quote
        return pair
    if exchange in ("Binance", "Bybit"):
        return pair.replace("/", "").replace("-", "")
    return pair


def get_price(symbol, exchange):
    if exchange not in EXCHANGE_APIS:
        exchange = "Binance"
    api = EXCHANGE_APIS[exchange]
    formatted = format_symbol(symbol, exchange)
    try:
        r = requests.get(api["url"].format(symbol=formatted), timeout=5)
        r.raise_for_status()
        data = r.json()
        if exchange == "KuCoin" and "data" in data:
            data = data["data"]
        elif exchange == "Bybit" and "result" in data:
            tickers = data["result"].get("list", [])
            data = tickers[0] if tickers else {}
        price = float(data.get(api["price_key"], 0))
        if price > 0:
            return price
    except Exception:
        pass
    return None


def send_telegram(message):
    """Send message to Telegram, splitting into chunks if > 4096 chars."""
    token = load_token(TG_TOKEN_FILE)
    if not token:
        print("[ERROR] No Telegram token")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Split into Telegram-safe chunks
    MAX_LEN = 4096
    chunks = []
    while len(message) > MAX_LEN:
        idx = message.rfind('\n', 0, MAX_LEN)
        if idx == -1:
            idx = MAX_LEN
        chunks.append(message[:idx])
        message = message[idx+1:]
    chunks.append(message)

    all_ok = True
    for i, chunk in enumerate(chunks):
        try:
            r = requests.post(url, json={
                "chat_id": CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML"
            }, timeout=15)
            if not r.json().get("ok", False):
                print(f"[ERROR] Telegram part {i+1} failed: {r.text}")
                all_ok = False
            if i < len(chunks) - 1:
                time.sleep(1)
        except Exception as e:
            print(f"[ERROR] Telegram: {e}")
            all_ok = False
    return all_ok


def build_report(signals, fetch_failures=0, skip_prices=False):
    wins   = [s for s in signals if s.get("status") == "WIN"]
    losses = [s for s in signals if s.get("status") == "LOSS"]
    opens  = [s for s in signals if s.get("status") == "OPEN"]
    closed = wins + losses

    total  = len(signals)
    wr     = (len(wins) / len(closed) * 100) if closed else 0
    lr     = (len(losses) / len(closed) * 100) if closed else 0
    net_pnl = sum(s.get("pnl_usd") or 0 for s in closed)
    capital_return = (net_pnl / (len(closed) * CAPITAL) * 100) if closed else 0

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    L = []
    L.append("📊 SIGNAL VALIDATOR REPORT")
    L.append(f"🕐 {now_str} UTC")
    L.append("━━━━━━━━━━━━━━━━━━━━")
    L.append("")
    L.append("📈 SUMMARY")
    L.append(f"Total Signals: {total} | ✅{len(wins)} | ❌{len(losses)} | ⏳{len(opens)}")
    if fetch_failures:
        L.append(f"⚠️ Fetch Failures: {fetch_failures}")
    L.append(f"🎯 Win Rate: {wr:.1f}%")
    L.append(f"📉 Loss Rate: {lr:.1f}%")
    pnl_sign = "+" if net_pnl >= 0 else ""
    L.append(f"💰 Net P&L: {pnl_sign}${net_pnl:,.2f} (@ ${CAPITAL:,.0f}/signal)")
    L.append(f"💵 Capital Return: {pnl_sign}{capital_return:.1f}%")

    # Per-pair breakdown — group by (pair, direction)
    pair_dir_stats = defaultdict(lambda: {"w": 0, "l": 0, "o": 0, "pnl": 0.0, "n": 0})
    for s in signals:
        pair = s.get("pair", "?")
        direction = s.get("direction", "?")
        key = (pair, direction)
        pair_dir_stats[key]["n"] += 1
        pair_dir_stats[key]["pnl"] += s.get("pnl_usd") or 0
        if s.get("status") == "WIN":
            pair_dir_stats[key]["w"] += 1
        elif s.get("status") == "LOSS":
            pair_dir_stats[key]["l"] += 1
        elif s.get("status") == "OPEN":
            pair_dir_stats[key]["o"] += 1

    sorted_pd = sorted(pair_dir_stats.items(), key=lambda x: (-x[1]["n"], -x[1]["pnl"]))

    L.append("")
    L.append("📋 ALL PAIRS BREAKDOWN")
    L.append("━━━━━━━━━━━━━━━━━━━━")
    for (pair, direction), ps in sorted_pd:
        parts = []
        if ps["w"]:
            parts.append("✅" + str(ps["w"]))
        if ps["l"]:
            parts.append("❌" + str(ps["l"]))
        if ps["o"]:
            parts.append("⏳" + str(ps["o"]))
        status_str = " ".join(parts) if parts else "—"
        pnl_s = ("+" if ps["pnl"] >= 0 else "") + f"${ps['pnl']:,.2f}"
        L.append(f"🔹 {pair} ({direction}) → {ps['n']} sig | {status_str} | {pnl_s}")

    # Top winners
    L.append("")
    L.append("🏆 TOP WINNERS")
    for s in sorted(wins, key=lambda x: x.get("pnl_usd") or 0, reverse=True)[:5]:
        pct = s.get("pnl_pct") or 0
        pnl = s.get("pnl_usd") or 0
        L.append(f"  ✅ {s['pair']} {s['direction']} → +${pnl:,.2f} (+{pct:.1f}%)")

    # Top losers
    L.append("")
    L.append("🔻 TOP LOSERS")
    for s in sorted(losses, key=lambda x: x.get("pnl_usd") or 0)[:5]:
        pct = s.get("pnl_pct") or 0
        pnl = s.get("pnl_usd") or 0
        L.append(f"  ❌ {s['pair']} {s['direction']} → -${abs(pnl):,.2f} ({pct:.1f}%)")

    # Still open with live P&L
    L.append("")
    L.append(f"⏳ STILL OPEN ({len(opens)})")
    if not skip_prices:
        for s in opens:
            entry     = s.get("entry", 0)
            direction = s.get("direction", "")
            exchange  = s.get("exchange", "KuCoin")
            pair      = s.get("pair", "")
            current = get_price(pair, exchange)
            if current and entry:
                if direction == "LONG":
                    upnl_pct = (current - entry) / entry * 100
                else:
                    upnl_pct = (entry - current) / entry * 100
                upnl_usd = round(CAPITAL * upnl_pct / 100, 2)
                L.append(
                    f"  ⏳ {pair} {direction} | Curr: {current:.6f} | "
                    f"PnL: {upnl_pct:+.2f}% ({upnl_usd:+,.0f})"
                )
            else:
                L.append(f"  ⏳ {pair} {direction} | Entry: {entry:.6f} | Price n/a")
    else:
        for s in opens:
            L.append(f"  ⏳ {s['pair']} {s.get('direction','')} | Entry: {s.get('entry',0):.6f}")

    return "\n".join(L)


def get_last_run_time():
    """Read the last successful run time from the tracker log."""
    tracker_file = os.path.join(SCRIPT_DIR, "win_loss_tracker.json")
    if os.path.exists(tracker_file):
        try:
            with open(tracker_file) as f:
                data = json.load(f)
            snaps = data.get("snapshots", [])
            if snaps:
                return datetime.fromisoformat(snaps[-1]["timestamp"])
        except Exception:
            pass
    return None

def run_gap_recovery(last_run):
    """If we were offline for >2 hours, run extra validator passes to catch up."""
    if last_run is None:
        return
    now = datetime.now(timezone.utc)
    gap_hours = (now - last_run).total_seconds() / 3600
    if gap_hours <= 2:
        return  # no significant gap

    print(f"[RECOVERY] Offline gap detected: {gap_hours:.1f}h since last run")
    print(f"[RECOVERY] Running extra validation passes...")

    # Run the validator multiple times with delays to catch price movements
    # that happened during the offline period
    import hermes_validator as _val
    for pass_num in range(min(int(gap_hours), 6)):  # max 6 extra passes
        print(f"[RECOVERY] Validation pass {pass_num+1}...")
        try:
            _val.validate_signals()
        except Exception as e:
            print(f"[RECOVERY] Pass {pass_num+1} error: {e}")
        if pass_num < int(gap_hours) - 1:
            time.sleep(5)  # small delay between passes

    print(f"[RECOVERY] Gap recovery complete")


def main():
    dry_run    = "--dry-run" in sys.argv
    skipPrices = "--skip-prices" in sys.argv or dry_run

    # ── Offline gap recovery ──
    if not dry_run:
        last_run = get_last_run_time()
        if last_run:
            gap_h = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
            if gap_h > 2:
                run_gap_recovery(last_run)

    # Step 1: Fetch from GitHub
    print("[1/5] Fetching signals from GitHub...")
    signals = gh_fetch_signals()
    open_count = len([s for s in signals if s.get("status") == "OPEN"])
    print(f"       {len(signals)} signals ({open_count} OPEN)")
    with open(SIGNAL_LOG_FILE, "w") as f:
        json.dump(signals, f, indent=2)

    # Step 2: Run validator (import directly to use venv Python)
    print("[2/5] Running validator...")
    import hermes_validator as _val
    import io, contextlib
    val_buf = io.StringIO()
    with contextlib.redirect_stdout(val_buf), contextlib.redirect_stderr(val_buf):
        _val.validate_signals()
    val_output = val_buf.getvalue()
    fetch_failures = val_output.count("[ERROR] All attempts failed")
    # Print last few lines of validator output
    val_lines = [l for l in val_output.strip().split('\n') if l]
    if val_lines:
        for l in val_lines[-5:]:
            print(f"       {l}")
    print(f"       Fetch failures: {fetch_failures}")

    # Step 3: Read updated signals
    print("[3/5] Reading updated signals...")
    with open(SIGNAL_LOG_FILE) as f:
        updated = json.load(f)

    # Step 4: Commit back to GitHub (non-fatal if it fails)
    print("[4/5] Committing to GitHub...")
    if not dry_run:
        try:
            if gh_commit_signals(updated):
                print("       Committed OK")
            else:
                print("       WARN: commit returned False — signals not synced to GitHub")
        except Exception as e:
            print(f"       WARN: GitHub commit failed ({e}) — signals not synced to GitHub")
            print("       The validator still updated the local signals_log.json")
    else:
        print("       DRY RUN — skip commit")

    # Step 5: Build and send report
    print("[5/5] Building report...")
    report = build_report(updated, fetch_failures, skipPrices)

    if dry_run:
        print("\n" + "=" * 50)
        print("REPORT PREVIEW:")
        print("=" * 50)
        print(report)
    else:
        if send_telegram(report):
            print("       Sent to Telegram OK")
        else:
            print("       ERROR: Telegram send failed")

    with open(os.path.join(SCRIPT_DIR, "last_hourly_report.txt"), "w") as f:
        f.write(report)

    # Step 6: Win/Loss tracker
    print("[6/6] Running win/loss tracker...")
    try:
        import subprocess as sp
        tracker_result = sp.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "win_loss_tracker.py")],
            capture_output=True, text=True, timeout=30
        )
        if tracker_result.stdout:
            print(tracker_result.stdout.strip())
    except Exception as e:
        print(f"       Tracker error (non-fatal): {e}")

    print("[DONE]")


if __name__ == "__main__":
    main()
