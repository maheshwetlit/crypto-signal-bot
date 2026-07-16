#!/usr/bin/env python3
"""
hermes_daily_report.py
Fetches signals_log.json from GitHub, calculates daily stats per token,
and sends a formatted report to Telegram.
"""
import os, sys, json, base64, urllib.request, requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# --- Config ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GH_TOKEN_FILE = os.path.join(SCRIPT_DIR, ".gh_token")
TG_TOKEN_FILE = os.path.join(SCRIPT_DIR, ".tg_token")
CHAT_ID = "5515185305"
CAPITAL = 1000.0

# --- Read tokens ---
with open(GH_TOKEN_FILE) as f:
    gh_token = f.read().strip()
with open(TG_TOKEN_FILE) as f:
    tg_token = f.read().strip()

# --- Fetch signals_log.json ---
url = "https://api.github.com/repos/maheshwetlit/crypto-signal-bot/contents/signals_log.json"
req = urllib.request.Request(url, headers={
    "Authorization": "Bearer " + gh_token,
    "Accept": "application/vnd.github.v3+json"
})
resp = urllib.request.urlopen(req, timeout=15)
data = json.loads(resp.read())
raw = base64.b64decode(data["content"]).decode()
signals = json.loads(raw)

def _classify(sig):
    """Robust WIN/LOSS/OPEN classification (status OR result aware)."""
    st = sig.get("status"); res = sig.get("result")
    if st == "WIN" or res == "WIN":
        return "WIN"
    if st == "LOSS" or res in ("LOSS", "STALE", "EXPIRED"):
        return "LOSS"
    if st == "CLOSED":
        pnl = sig.get("pnl_usd")
        if pnl is not None:
            return "WIN" if pnl > 0 else "LOSS"
        return "LOSS"
    if st == "OPEN":
        return "OPEN"
    if res == "WIN":
        return "WIN"
    return "OPEN"


# --- Filter to yesterday ---
yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
filtered = [s for s in signals if yesterday in s.get("time", "")]

total = len(filtered)
wins = sum(1 for s in filtered if _classify(s) == "WIN")
losses = sum(1 for s in filtered if _classify(s) == "LOSS")
opens = sum(1 for s in filtered if _classify(s) == "OPEN")
closed = wins + losses
wr = (wins / closed * 100) if closed > 0 else 0
pnl = sum((s.get("pnl_usd") or 0) for s in filtered)

# --- Per-token breakdown ---
tokens = defaultdict(lambda: {"w": 0, "l": 0, "o": 0, "pnl": 0.0, "n": 0})
for s in filtered:
    t = s.get("pair", "?")
    tokens[t]["n"] += 1
    tokens[t]["pnl"] += s.get("pnl_usd") or 0
    c = _classify(s)
    if c == "WIN":
        tokens[t]["w"] += 1
    elif c == "LOSS":
        tokens[t]["l"] += 1
    elif c == "OPEN":
        tokens[t]["o"] += 1

# --- Build Telegram message ---
lines = []
lines.append("📊 <b>DAILY SIGNAL REPORT — " + yesterday + "</b>")
lines.append("━━━━━━━━━━━━━━━━━━━━")
lines.append("📈 Signals: <b>" + str(total) + "</b> | ✅ " + str(wins) + " | ❌ " + str(losses) + " | ⏳ " + str(opens))
lines.append("🎯 Win Rate: <b>" + "{:.1f}".format(wr) + "%</b>")
pnl_str = "+" + "{:.2f}".format(pnl) if pnl >= 0 else "{:.2f}".format(pnl)
lines.append("💰 Net P&L: <b>$" + pnl_str + "</b> (@ $1,000/signal)")

if tokens:
    lines.append("")
    lines.append("📋 <b>BREAKDOWN BY TOKEN</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    for t, v in sorted(tokens.items(), key=lambda x: -x[1]["pnl"]):
        tp = "+" + "{:.2f}".format(v["pnl"]) if v["pnl"] >= 0 else "{:.2f}".format(v["pnl"])
        status_parts = []
        if v["w"]: status_parts.append("✅" + str(v["w"]))
        if v["l"]: status_parts.append("❌" + str(v["l"]))
        if v["o"]: status_parts.append("⏳" + str(v["o"]))
        status_str = " ".join(status_parts) if status_parts else "—"
        lines.append("🔹 <b>" + t + "</b> → " + str(v["n"]) + " sig | " + status_str + " | $" + tp)

message = "\n".join(lines)

# --- Send to Telegram ---
tg_url = "https://api.telegram.org/bot" + tg_token + "/sendMessage"
r = requests.post(tg_url, json={
    "chat_id": CHAT_ID,
    "text": message,
    "parse_mode": "HTML"
}, timeout=10)

if r.json().get("ok"):
    print("[OK] Daily report sent to Telegram for " + yesterday)
else:
    print("[ERROR] Telegram send failed:", r.json())
    sys.exit(1)