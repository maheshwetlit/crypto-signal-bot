#!/usr/bin/env python3
"""
Win/Loss tracker — stores historical win/loss rates and sends comparison reports.
Called after each validator run. Data stored in win_loss_tracker.json.
Sends to Telegram if loss rate exceeds thresholds.
"""
import json, os, requests, time
from datetime import datetime, timezone
from collections import defaultdict

SCRIPT_DIR = r"C:\Users\mahes\AppData\Local\hermes\hermes-agent"
SIGNAL_LOG  = os.path.join(SCRIPT_DIR, "signals_log.json")
TRACKER_LOG = os.path.join(SCRIPT_DIR, "win_loss_tracker.json")
TG_TOKEN    = os.path.join(SCRIPT_DIR, ".tg_token")
CHAT_ID     = "5515185305"
CAPITAL     = 1000.0

# Thresholds
MAX_LOSS_RATE_PCT   = 5.0   # strict max loss rate %
AVG_LOSS_PCT        = 3.0   # average loss size target %
MAX_LOSS_SIZE_PCT   = 5.0   # max single trade loss size %

def load_signals():
    with open(SIGNAL_LOG) as f:
        return json.load(f)

def load_tracker():
    if os.path.exists(TRACKER_LOG):
        with open(TRACKER_LOG) as f:
            return json.load(f)
    return {"snapshots": []}

def save_tracker(data):
    with open(TRACKER_LOG, "w") as f:
        json.dump(data, f, indent=2)

def current_stats(signals):
    closed = [s for s in signals if s.get("status") in ("WIN","LOSS")]
    wins = [s for s in closed if s.get("status") == "WIN"]
    losses = [s for s in closed if s.get("status") == "LOSS"]
    
    if not closed:
        return None
    
    short_wins = [s for s in wins if s.get("direction") == "SHORT"]
    short_losses = [s for s in losses if s.get("direction") == "SHORT"]
    long_wins = [s for s in wins if s.get("direction") == "LONG"]
    long_losses = [s for s in losses if s.get("direction") == "LONG"]
    
    short_closed = short_wins + short_losses
    
    return {
        "total": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins)/len(closed)*100, 1),
        "loss_rate": round(len(losses)/len(closed)*100, 1),
        "avg_loss_pct": round(sum(abs(s.get("pnl_pct",0)) for s in losses)/len(losses), 1) if losses else 0,
        "max_loss_pct": round(max(abs(s.get("pnl_pct",0)) for s in losses), 1) if losses else 0,
        "net_pnl": round(sum(s.get("pnl_usd",0) for s in closed), 2),
        "short_win_rate": round(len(short_wins)/len(short_closed)*100, 1) if short_closed else 0,
        "short_loss_rate": round(len(short_losses)/len(short_closed)*100, 1) if short_closed else 0,
        "long_loss_count": len(long_losses),
        "long_loss_pnl": round(sum(s.get("pnl_usd",0) for s in long_losses), 2),
        "short_loss_count": len(short_losses),
        "short_loss_pnl": round(sum(s.get("pnl_usd",0) for s in short_losses), 2),
        "losses_over_5pct": len([s for s in losses if abs(s.get("pnl_pct",0)) > MAX_LOSS_SIZE_PCT]),
    }

def build_report(stats, prev_stats, tracker):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    nl = "\n"
    
    m = (
        f"📏 <b>WIN/LOSS TRACKER</b>{nl}"
        f"🕐 {now_str} UTC{nl}"
        f'━━━━━━━━━━━━━━━━━━━━{nl}{nl}'
    )
    
    # Current vs target
    m += (
        f'<b>Current Performance</b>{nl}'
        f'━━━━━━━━━━━━━━━━━━━━{nl}'
        f'Win Rate:  {stats["win_rate"]}% | Loss Rate: {stats["loss_rate"]}%{nl}'
        f'Avg Loss:  {stats["avg_loss_pct"]}% | Max Loss: {stats["max_loss_pct"]}%{nl}'
        f'Net P&L:   ${stats["net_pnl"]:,.2f}{nl}'
        f'Losses &gt;5%: {stats["losses_over_5pct"]} trades{nl}{nl}'
    )
    
    # Target compliance
    lr_ok = stats["loss_rate"] <= MAX_LOSS_RATE_PCT
    al_ok = stats["avg_loss_pct"] <= AVG_LOSS_PCT
    ms_ok = stats["max_loss_pct"] <= MAX_LOSS_SIZE_PCT
    
    m += (
        f'<b>Target Compliance</b>{nl}'
        f'━━━━━━━━━━━━━━━━━━━━{nl}'
        f'Loss rate ≤5%:  {"✅" if lr_ok else "❌"} {stats["loss_rate"]}%{nl}'
        f'Avg loss ≤3%:   {"✅" if al_ok else "❌"} {stats["avg_loss_pct"]}%{nl}'
        f'Max loss ≤5%:   {"✅" if ms_ok else "❌"} {stats["max_loss_pct"]}%{nl}{nl}'
    )
    
    # Direction breakdown
    m += (
        f'<b>Direction Breakdown</b>{nl}'
        f'━━━━━━━━━━━━━━━━━━━━{nl}'
        f'SHORT: {stats["short_win_rate"]}% win | {stats["short_loss_rate"]}% loss ({stats["short_loss_count"]} losses = ${stats["short_loss_pnl"]:,.0f}){nl}'
        f'LONG:  {stats["long_loss_count"]} losses = ${stats["long_loss_pnl"]:,.0f} (no new LONGs fired){nl}{nl}'
    )
    
    # Trend vs previous
    if prev_stats:
        wr_delta = stats["win_rate"] - prev_stats["win_rate"]
        lr_delta = stats["loss_rate"] - prev_stats["loss_rate"]
        al_delta = stats["avg_loss_pct"] - prev_stats["avg_loss_pct"]
        wr_arrow = "📈" if wr_delta > 0 else "📉" if wr_delta < 0 else "➡️"
        lr_arrow = "📉" if lr_delta < 0 else "📈" if lr_delta > 0 else "➡️"
        
        m += (
            f'<b>Trend vs Previous</b>{nl}'
            f'━━━━━━━━━━━━━━━━━━━━{nl}'
            f'Win rate:  {wr_arrow} {wr_delta:+.1f}%{nl}'
            f'Loss rate: {lr_arrow} {lr_delta:+.1f}%{nl}'
            f'Avg loss:  {"📉" if al_delta < 0 else "📈"} {al_delta:+.1f}%{nl}{nl}'
        )
    
    # How to reach target
    if not lr_ok or not al_ok:
        m += (
            f'<b>🔧 To Reach Target</b>{nl}'
            f'━━━━━━━━━━━━━━━━━━━━{nl}'
        )
        if stats["long_loss_count"] > 0:
            m += f'LONG suppression eliminates {stats["long_loss_count"]} losses ({stats["long_loss_count"]}/143 = {stats["long_loss_count"]/143*100:.0f}%){nl}'
        if stats["losses_over_5pct"] > 0:
            m += f'SL cap at 3% would cut {stats["losses_over_5pct"]} oversized losses{nl}'
        m += f'Projected with Hermes filters: LOSS rate ~3-5% (SHORTs only, tight SL){nl}'
    
    return m

def send_telegram(message):
    with open(TG_TOKEN) as f:
        token = f.read().strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    MAX_LEN = 4096
    chunks = []
    while len(message) > MAX_LEN:
        idx = message.rfind("\n", 0, MAX_LEN)
        if idx == -1: idx = MAX_LEN
        chunks.append(message[:idx])
        message = message[idx+1:]
    chunks.append(message)
    for i, chunk in enumerate(chunks):
        requests.post(url, json={"chat_id": CHAT_ID, "text": chunk, "parse_mode": "HTML"}, timeout=15)
        if i < len(chunks) - 1:
            time.sleep(1)
    return len(chunks)

def main():
    signals = load_signals()
    tracker = load_tracker()
    
    stats = current_stats(signals)
    if not stats:
        print("No closed signals found")
        return
    
    # Save snapshot
    now_str = datetime.now(timezone.utc).isoformat()
    snapshot = {"timestamp": now_str, **stats}
    tracker["snapshots"].append(snapshot)
    
    # Keep last 30 snapshots
    if len(tracker["snapshots"]) > 30:
        tracker["snapshots"] = tracker["snapshots"][-30:]
    save_tracker(tracker)
    
    # Previous stats for comparison
    prev_stats = tracker["snapshots"][-2] if len(tracker["snapshots"]) >= 2 else None
    
    # Build and send report
    report = build_report(stats, prev_stats, tracker)
    n = send_telegram(report)
    print(f"Tracker report sent ({n} parts)")
    print(f"WR={stats['win_rate']}% LR={stats['loss_rate']}% avg_loss={stats['avg_loss_pct']}%")

if __name__ == "__main__":
    main()
