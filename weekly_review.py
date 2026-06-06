#!/usr/bin/env python3
"""
Hermes Weekly Deep Review
Runs every Monday at 06:00 UTC.
Full parameter review, pair performance analysis, 
correlation check, and parameter optimization suggestions.
"""
import json, os, requests, time
from datetime import datetime, timezone
from collections import defaultdict

SCRIPT_DIR = r"C:\Users\mahes\AppData\Local\hermes\hermes-agent"
SIGNAL_LOG  = os.path.join(SCRIPT_DIR, "signals_log.json")
CONFIG_LOG  = os.path.join(SCRIPT_DIR, "hermes_config_history.json")
TG_TOKEN = os.path.join(SCRIPT_DIR, ".tg_token")
CHAT_ID     = "5515185305"
CAPITAL     = 1000.0

def load_signals():
    with open(SIGNAL_LOG) as f:
        return json.load(f)

def load_config_history():
    if os.path.exists(CONFIG_LOG):
        with open(CONFIG_LOG) as f:
            return json.load(f)
    return {"snapshots": []}

def pair_performance(signals):
    """Analyze performance by pair."""
    closed = [s for s in signals if s.get("status") in ("WIN","LOSS")]
    pairs = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0, "pnl_pct_sum": 0.0})
    for s in closed:
        p = s.get("pair", "?")
        pairs[p]["w"] += 1 if s.get("status") == "WIN" else 0
        pairs[p]["l"] += 1 if s.get("status") == "LOSS" else 0
        pairs[p]["pnl"] += s.get("pnl_usd", 0)
        pairs[p]["pnl_pct_sum"] += abs(s.get("pnl_pct", 0))
    return pairs

def style_performance(signals):
    """Analyze performance by style (PRIME/BREAKOUT/REVERSAL/MOMENTUM)."""
    closed = [s for s in signals if s.get("status") in ("WIN","LOSS")]
    styles = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for s in closed:
        style = s.get("style", "?")
        styles[style]["w"] += 1 if s.get("status") == "WIN" else 0
        styles[style]["l"] += 1 if s.get("status") == "LOSS" else 0
        styles[style]["pnl"] += s.get("pnl_usd", 0)
    return styles

def adx_bucket_performance(signals):
    """Analyze performance by ADX range."""
    closed = [s for s in signals if s.get("status") in ("WIN","LOSS")]
    buckets = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for s in closed:
        adx = s.get("adx", 0)
        if adx < 20:
            bucket = "ADX<20"
        elif adx < 25:
            bucket = "ADX 20-25"
        elif adx < 30:
            bucket = "ADX 25-30"
        elif adx < 35:
            bucket = "ADX 30-35"
        else:
            bucket = "ADX 35+"
        buckets[bucket]["w"] += 1 if s.get("status") == "WIN" else 0
        buckets[bucket]["l"] += 1 if s.get("status") == "LOSS" else 0
        buckets[bucket]["pnl"] += s.get("pnl_usd", 0)
    return buckets

def sl_distance_performance(signals):
    """Analyze performance by SL distance %."""
    closed = [s for s in signals if s.get("status") in ("WIN","LOSS")]
    buckets = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for s in closed:
        entry = s.get("entry", 0)
        sl = s.get("sl", 0)
        if entry and sl:
            sl_pct = abs(entry - sl) / entry * 100
            if sl_pct < 1.0:
                bucket = "SL<1%"
            elif sl_pct < 2.0:
                bucket = "SL 1-2%"
            elif sl_pct < 3.0:
                bucket = "SL 2-3%"
            else:
                bucket = "SL 3%+"
            buckets[bucket]["w"] += 1 if s.get("status") == "WIN" else 0
            buckets[bucket]["l"] += 1 if s.get("status") == "LOSS" else 0
            buckets[bucket]["pnl"] += s.get("pnl_usd", 0)
    return buckets

def trend_analysis(config_history):
    """Analyze improvement trend over snapshots."""
    snaps = config_history.get("snapshots", [])
    if len(snaps) < 2:
        return None
    recent = snaps[-7:]  # last 7 days
    return {
        "days": len(recent),
        "wr_start": recent[0]["win_rate"],
        "wr_end": recent[-1]["win_rate"],
        "lr_start": recent[0]["loss_rate"],
        "lr_end": recent[-1]["loss_rate"],
        "pnl_start": recent[0]["net_pnl"],
        "pnl_end": recent[-1]["net_pnl"],
    }

def build_report(signals, config_history):
    closed = [s for s in signals if s.get("status") in ("WIN","LOSS")]
    wins = [s for s in closed if s.get("status") == "WIN"]
    losses = [s for s in closed if s.get("status") == "LOSS"]
    opens = [s for s in signals if s.get("status") == "OPEN"]

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    nl = "\n"

    m = (
        f"📊 <b>HERMES WEEKLY REVIEW</b>{nl}"
        f"🕐 {now_str} UTC{nl}"
        f"━━━━━━━━━━━━━━━━━━━━{nl}{nl}"
        f"<b>Overall: {len(closed)} closed | {len(opens)} open</b>{nl}"
        f"WR: {len(wins)/len(closed)*100:.1f}% | LR: {len(losses)/len(closed)*100:.1f}%{nl}"
        f"Net P&L: ${sum(s.get('pnl_usd',0) for s in closed):,.0f}{nl}{nl}"
    )

    # Pair performance
    pairs = pair_performance(signals)
    m += f"<b>📋 Pair Performance (top 10 by trade count)</b>{nl}"
    sorted_pairs = sorted(pairs.items(), key=lambda x: -(x[1]["w"] + x[1]["l"]))
    for pair, ps in sorted_pairs[:10]:
        n = ps["w"] + ps["l"]
        wr = ps["w"] / n * 100 if n > 0 else 0
        m += f"  {pair:15s} {n:3d} sig | WR {wr:.0f}% | ${ps['pnl']:,.0f}{nl}"

    # Best/worst pairs
    profitable = [(p, ps) for p, ps in pairs.items() if ps["pnl"] > 0]
    unprofitable = [(p, ps) for p, ps in pairs.items() if ps["pnl"] < 0]
    if profitable:
        m += f"\n<b>🏆 Best pairs:</b>{nl}"
        for pair, ps in sorted(profitable, key=lambda x: -x[1]["pnl"])[:5]:
            m += f"  {pair:15s} ${ps['pnl']:,.0f}{nl}"
    if unprofitable:
        m += f"\n<b>🔻 Worst pairs:</b>{nl}"
        for pair, ps in sorted(unprofitable, key=lambda x: x[1]["pnl"])[:5]:
            m += f"  {pair:15s} ${ps['pnl']:,.0f}{nl}"

    # Style performance
    styles = style_performance(signals)
    m += f"\n<b>📐 Style Performance</b>{nl}"
    for style, ps in sorted(styles.items(), key=lambda x: -x[1]["pnl"]):
        n = ps["w"] + ps["l"]
        wr = ps["w"] / n * 100 if n > 0 else 0
        m += f"  {style:10s} {n:3d} sig | WR {wr:.0f}% | ${ps['pnl']:,.0f}{nl}"

    # ADX bucket performance
    adx_buckets = adx_bucket_performance(signals)
    m += f"\n<b>📈 ADX Bucket Performance</b>{nl}"
    for bucket in ["ADX<20", "ADX 20-25", "ADX 25-30", "ADX 30-35", "ADX 35+"]:
        if bucket in adx_buckets:
            ps = adx_buckets[bucket]
            n = ps["w"] + ps["l"]
            wr = ps["w"] / n * 100 if n > 0 else 0
            m += f"  {bucket:12s} {n:3d} sig | WR {wr:.0f}% | ${ps['pnl']:,.0f}{nl}"

    # SL distance performance
    sl_buckets = sl_distance_performance(signals)
    m += f"\n<b>📏 SL Distance Performance</b>{nl}"
    for bucket in ["SL<1%", "SL 1-2%", "SL 2-3%", "SL 3%+"]:
        if bucket in sl_buckets:
            ps = sl_buckets[bucket]
            n = ps["w"] + ps["l"]
            wr = ps["w"] / n * 100 if n > 0 else 0
            m += f"  {bucket:10s} {n:3d} sig | WR {wr:.0f}% | ${ps['pnl']:,.0f}{nl}"

    # Trend
    trend = trend_analysis(config_history)
    if trend:
        m += f"\n<b>📅 {trend['days']}-Day Trend</b>{nl}"
        wr_d = trend["wr_end"] - trend["wr_start"]
        lr_d = trend["lr_end"] - trend["lr_start"]
        pnl_d = trend["pnl_end"] - trend["pnl_start"]
        m += f"  WR: {trend['wr_start']:.1f}% → {trend['wr_end']:.1f}% ({wr_d:+.1f}%){nl}"
        m += f"  LR: {trend['lr_start']:.1f}% → {trend['lr_end']:.1f}% ({lr_d:+.1f}%){nl}"
        m += f"  P&L: ${trend['pnl_start']:,.0f} → ${trend['pnl_end']:,.0f} (${pnl_d:+,.0f}){nl}"

    # Recommendations
    m += f"\n<b>💡 Recommendations</b>{nl}"
    # Find underperforming pairs
    bad_pairs = [(p, ps) for p, ps in pairs.items() if ps["l"] >= 2 and ps["pnl"] < -100]
    if bad_pairs:
        m += f"  Consider blocking: {', '.join(p for p,_ in bad_pairs[:5])}{nl}"
    # Find best ADX range
    best_adx = max(adx_buckets.items(), key=lambda x: x[1]["pnl"]) if adx_buckets else None
    if best_adx:
        m += f"  Best ADX range: {best_adx[0]} (${best_adx[1]['pnl']:,.0f}){nl}"
    # Find best SL range
    best_sl = max(sl_buckets.items(), key=lambda x: x[1]["pnl"]) if sl_buckets else None
    if best_sl:
        m += f"  Best SL range: {best_sl[0]} (${best_sl[1]['pnl']:,.0f}){nl}"

    return m

def main():
    signals = load_signals()
    config_history = load_config_history()
    report = build_report(signals, config_history)

    with open(TG_TOKEN) as f:
        token = f.read().strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    MAX_LEN = 4096
    chunks = []
    while len(report) > MAX_LEN:
        idx = report.rfind("\n", 0, MAX_LEN)
        if idx == -1: idx = MAX_LEN
        chunks.append(report[:idx])
        report = report[idx+1:]
    chunks.append(report)
    for i, chunk in enumerate(chunks):
        requests.post(url, json={"chat_id": CHAT_ID, "text": chunk, "parse_mode": "HTML"}, timeout=15)
        if i < len(chunks) - 1:
            time.sleep(1)
    print(f"Weekly review sent ({len(chunks)} parts)")

if __name__ == "__main__":
    main()
