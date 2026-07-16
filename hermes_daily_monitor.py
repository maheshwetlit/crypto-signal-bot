#!/usr/bin/env python3
"""
Hermes Performance Monitor & Auto-Adjuster
Runs daily at 05:00 UTC. Tracks all key metrics, compares to targets,
auto-adjusts parameters if loss rate exceeds thresholds, sends report.
"""
import json, os, requests, time, subprocess, sys
from datetime import datetime, timezone
from collections import defaultdict

SCRIPT_DIR = r"C:\Users\mahes\AppData\Local\hermes\hermes-agent"
SIGNAL_LOG  = os.path.join(SCRIPT_DIR, "signals_log.json")
TRACKER_LOG = os.path.join(SCRIPT_DIR, "win_loss_tracker.json")
CONFIG_LOG  = os.path.join(SCRIPT_DIR, "hermes_config_history.json")
TG_TOKEN = os.path.join(SCRIPT_DIR, ".tg_token")
CHAT_ID     = "5515185305"
CAPITAL     = 1000.0

# ═══════════════════════════════════════════
#  TARGETS (world-class trading script)
# ═══════════════════════════════════════════
TARGETS = {
    "win_rate_min":         80.0,    # minimum win rate %
    "loss_rate_max":         5.0,    # maximum loss rate %
    "loss_rate_target":      3.0,    # target loss rate %
    "avg_loss_pct_max":      3.0,    # max avg loss per trade %
    "max_loss_pct_max":      5.0,    # max single trade loss %
    "expectancy_min":        50.0,   # min expectancy per trade $
    "sharpe_min":            2.0,    # minimum sharpe-like ratio
    "max_drawdown_pct":      10.0,   # max acceptable drawdown %
}

# ═══════════════════════════════════════════
#  Data loading
# ═══════════════════════════════════════════
def load_signals():
    with open(SIGNAL_LOG) as f:
        return json.load(f)

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ═══════════════════════════════════════════
#  Core statistics
# ═══════════════════════════════════════════
def compute_stats(signals):
    closed = [s for s in signals if s.get("status") in ("WIN","LOSS")]
    wins = [s for s in closed if s.get("status") == "WIN"]
    losses = [s for s in closed if s.get("status") == "LOSS"]
    opens = [s for s in signals if s.get("status") == "OPEN"]

    if not closed:
        return None

    short_closed = [s for s in closed if s.get("direction") == "SHORT"]
    short_wins = [s for s in short_closed if s.get("status") == "WIN"]
    short_losses = [s for s in short_closed if s.get("status") == "LOSS"]

    win_pnls = [s.get("pnl_usd", 0) for s in wins]
    loss_pnls = [s.get("pnl_usd", 0) for s in losses]
    all_pnls = win_pnls + loss_pnls

    # Signal lifetimes
    lifetimes = []
    for s in closed:
        t_open = s.get("time", "")
        t_close = s.get("exit_time") or s.get("closed_at", "")
        if t_open and t_close:
            try:
                dt_o = datetime.fromisoformat(t_open.replace("Z", "+00:00"))
                dt_c = datetime.fromisoformat(t_close.replace("Z", "+00:00"))
                lifetimes.append({
                    "hours": (dt_c - dt_o).total_seconds() / 3600,
                    "result": s.get("status"),
                    "pnl": s.get("pnl_usd", 0),
                    "pnl_pct": s.get("pnl_pct", 0),
                    "direction": s.get("direction", ""),
                })
            except:
                pass

    # Consecutive losses
    consec_losses = 0
    max_consec_losses = 0
    for s in closed:
        if s.get("status") == "LOSS":
            consec_losses += 1
            max_consec_losses = max(max_consec_losses, consec_losses)
        else:
            consec_losses = 0

    # Drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for s in closed:
        cumulative += s.get("pnl_usd", 0)
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)

    # Sharpe-like ratio (avg pnl / std dev)
    avg_pnl = sum(all_pnls) / len(all_pnls) if all_pnls else 0
    variance = sum((p - avg_pnl)**2 for p in all_pnls) / len(all_pnls) if all_pnls else 0
    std_pnl = variance ** 0.5
    sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_closed": len(closed),
        "total_open": len(opens),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "loss_rate": round(len(losses) / len(closed) * 100, 1),
        "net_pnl": round(sum(all_pnls), 2),
        "avg_win": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0,
        "avg_loss": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0,
        "avg_loss_pct": round(sum(abs(s.get("pnl_pct", 0)) for s in losses) / len(losses), 1) if losses else 0,
        "max_loss_pct": round(max(abs(s.get("pnl_pct", 0)) for s in losses), 1) if losses else 0,
        "max_win_pct": round(max(s.get("pnl_pct", 0) for s in wins), 1) if wins else 0,
        "expectancy": round(avg_pnl, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(max_dd, 2),
        "max_consec_losses": max_consec_losses,
        "avg_lifetime_h": round(sum(l["hours"] for l in lifetimes) / len(lifetimes), 1) if lifetimes else 0,
        "win_lifetime_h": round(sum(l["hours"] for l in lifetimes if l["result"]=="WIN") / len([l for l in lifetimes if l["result"]=="WIN"]), 1) if any(l["result"]=="WIN" for l in lifetimes) else 0,
        "loss_lifetime_h": round(sum(l["hours"] for l in lifetimes if l["result"]=="LOSS") / len([l for l in lifetimes if l["result"]=="LOSS"]), 1) if any(l["result"]=="LOSS" for l in lifetimes) else 0,
        "short_win_rate": round(len(short_wins) / len(short_closed) * 100, 1) if short_closed else 0,
        "short_loss_rate": round(len(short_losses) / len(short_closed) * 100, 1) if short_closed else 0,
        "long_loss_count": len([s for s in losses if s.get("direction") == "LONG"]),
        "losses_over_5pct": len([s for s in losses if abs(s.get("pnl_pct", 0)) > 5.0]),
        "losses_over_3pct": len([s for s in losses if abs(s.get("pnl_pct", 0)) > 3.0]),
    }

# ═══════════════════════════════════════════
#  Auto-adjustment logic
# ═══════════════════════════════════════════
def auto_adjust(stats, config_history):
    """Analyze performance and suggest parameter adjustments."""
    adjustments = []
    severity = "OK"

    # Check loss rate
    if stats["loss_rate"] > TARGETS["loss_rate_max"]:
        severity = "CRITICAL"
        adjustments.append({
            "param": "SCORE_ENTRY_THR",
            "current": 85.0,
            "suggested": 88.0,
            "reason": f"Loss rate {stats['loss_rate']}% > target {TARGETS['loss_rate_max']}% — raise entry bar"
        })
        adjustments.append({
            "param": "MAX_SL_PCT",
            "current": 3.0,
            "suggested": 2.5,
            "reason": f"Loss rate {stats['loss_rate']}% — tighten SL cap from 3% to 2.5%"
        })
    elif stats["loss_rate"] > TARGETS["loss_rate_target"]:
        severity = "WARNING"
        adjustments.append({
            "param": "SCORE_ENTRY_THR",
            "current": 85.0,
            "suggested": 86.0,
            "reason": f"Loss rate {stats['loss_rate']}% > target {TARGETS['loss_rate_target']}% — slight raise"
        })

    # Check avg loss size
    if stats["avg_loss_pct"] > TARGETS["avg_loss_pct_max"]:
        if severity != "CRITICAL":
            severity = "WARNING"
        # Tighten SL proportionally to how far over target we are
        # Use 2.5% floor (not 2.0%) — crypto wicks are 1-2%, need room to breathe
        suggested = max(2.5, 3.0 - (stats["avg_loss_pct"] - TARGETS["avg_loss_pct_max"]) * 0.3)
        if suggested < 3.0:  # only suggest if it actually changes
            adjustments.append({
                "param": "MAX_SL_PCT",
                "current": 3.0,
                "suggested": round(suggested, 1),
                "reason": f"Avg loss {stats['avg_loss_pct']}% > target {TARGETS['avg_loss_pct_max']}% — tighten SL to {suggested:.1f}%"
            })

    # Check max single loss
    if stats["max_loss_pct"] > TARGETS["max_loss_pct_max"]:
        # Hard cap at 80% of max allowed (e.g., 4.0% if max is 5%)
        suggested = TARGETS["max_loss_pct_max"] * 0.8
        if suggested < 3.0:  # only suggest if it actually tightens
            adjustments.append({
                "param": "MAX_SL_PCT",
                "current": 3.0,
                "suggested": round(suggested, 1),
                "reason": f"Max loss {stats['max_loss_pct']}% > cap {TARGETS['max_loss_pct_max']}% — hard cap at {suggested:.1f}%"
            })

    # Check consecutive losses
    if stats["max_consec_losses"] >= 3:
        adjustments.append({
            "param": "BASE_COOLDOWN",
            "current": 900,
            "suggested": 1800,
            "reason": f"{stats['max_consec_losses']} consecutive losses — double cooldown to 30min"
        })

    # Check drawdown
    if stats["max_drawdown"] > CAPITAL * 5 * (TARGETS["max_drawdown_pct"] / 100):
        adjustments.append({
            "param": "MAX_OPEN_TOTAL",
            "current": 5,
            "suggested": 3,
            "reason": f"Max drawdown \${stats['max_drawdown']:,.0f} — reduce concurrent positions"
        })

    return severity, adjustments

# ═══════════════════════════════════════════
#  Report builder
# ═══════════════════════════════════════════
def build_report(stats, prev_stats, severity, adjustments, config_history):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    nl = "\n"

    # Severity emoji
    sev_emoji = {"OK": "🟢", "WARNING": "🟡", "CRITICAL": "🔴"}[severity]

    m = (
        f"{sev_emoji} <b>HERMES DAILY REPORT</b>{nl}"
        f"🕐 {now_str} UTC{nl}"
        f"━━━━━━━━━━━━━━━━━━━━{nl}{nl}"
        f"<b>📈 Performance</b>{nl}"
        f"━━━━━━━━━━━━━━━━━━━━{nl}"
        f"Win Rate:  {stats['win_rate']}% (target ≥{TARGETS['win_rate_min']}%) {'✅' if stats['win_rate'] >= TARGETS['win_rate_min'] else '❌'}{nl}"
        f"Loss Rate: {stats['loss_rate']}% (target ≤{TARGETS['loss_rate_target']}%) {'✅' if stats['loss_rate'] <= TARGETS['loss_rate_target'] else '❌'}{nl}"
        f"Avg Win:   +${stats['avg_win']:,.0f} ({stats['max_win_pct']:.1f}% max){nl}"
        f"Avg Loss:  ${stats['avg_loss']:,.0f} (target ≥-${CAPITAL * TARGETS['avg_loss_pct_max']/100:.0f}) {'✅' if abs(stats['avg_loss']) <= CAPITAL * TARGETS['avg_loss_pct_max']/100 else '❌'}{nl}"
        f"Max Loss:  {stats['max_loss_pct']}% (target ≤{TARGETS['max_loss_pct_max']}%) {'✅' if stats['max_loss_pct'] <= TARGETS['max_loss_pct_max'] else '❌'}{nl}"
        f"Net P&L:   ${stats['net_pnl']:,.2f}{nl}"
        f"Expectancy: ${stats['expectancy']:,.0f}/trade (target ≥${TARGETS['expectancy_min']}) {'✅' if stats['expectancy'] >= TARGETS['expectancy_min'] else '❌'}{nl}"
        f"Sharpe:    {stats['sharpe']:.2f} (target ≥{TARGETS['sharpe_min']}) {'✅' if stats['sharpe'] >= TARGETS['sharpe_min'] else '❌'}{nl}"
        f"Max DD:    ${stats['max_drawdown']:,.0f}{nl}"
        f"Max Consec Losses: {stats['max_consec_losses']}{nl}{nl}"
    )

    # Direction breakdown
    m += (
        f"<b>📊 Direction Breakdown</b>{nl}"
        f"━━━━━━━━━━━━━━━━━━━━{nl}"
        f"SHORT: {stats['short_win_rate']}% win | {stats['short_loss_rate']}% loss{nl}"
        f"LONG:  {stats['long_loss_count']} losses remaining (no new LONGs){nl}"
        f"Losses >3%: {stats['losses_over_3pct']} | >5%: {stats['losses_over_5pct']}{nl}{nl}"
    )

    # Signal lifetime
    m += (
        f"<b>⏱️ Signal Lifetime</b>{nl}"
        f"━━━━━━━━━━━━━━━━━━━━{nl}"
        f"Avg all:    {stats['avg_lifetime_h']:.0f}h{nl}"
        f"Avg wins:   {stats['win_lifetime_h']:.0f}h{nl}"
        f"Avg losses: {stats['loss_lifetime_h']:.0f}h{nl}{nl}"
    )

    # Trend vs previous
    if prev_stats:
        wr_d = stats['win_rate'] - prev_stats['win_rate']
        lr_d = stats['loss_rate'] - prev_stats['loss_rate']
        pnl_d = stats['net_pnl'] - prev_stats['net_pnl']
        m += (
            f"<b>📈 Trend (vs yesterday)</b>{nl}"
            f"━━━━━━━━━━━━━━━━━━━━{nl}"
            f"Win rate:  {'📈' if wr_d > 0 else '📉' if wr_d < 0 else '➡️'} {wr_d:+.1f}%{nl}"
            f"Loss rate: {'📉' if lr_d < 0 else '📈' if lr_d > 0 else '➡️'} {lr_d:+.1f}%{nl}"
            f"Net P&L:   {'📈' if pnl_d > 0 else '📉'} ${pnl_d:+,.0f}{nl}{nl}"
        )

    # Auto-adjustments (NOTE: these are SUGGESTIONS ONLY — the monitor never
    # writes them back to crypto_signal_bot.py. The bot keeps its own config.)
    if adjustments:
        m += (
            f"<b>🔧 Suggested Parameter Tuning ({severity})</b>{nl}"
            f"━━━━━━━━━━━━━━━━━━━━{nl}"
            f"<i>⚠️ Suggestions only — NOT auto-applied to the bot.</i>{nl}{nl}"
        )
        for adj in adjustments:
            m += f"• {adj['param']}: suggested {adj['suggested']:.1f} (current {adj['current']}){nl}"
            m += f"  Reason: {adj['reason']}{nl}"
    else:
        m += (
            f"<b>🔧 Auto-Adjustments</b>{nl}"
            f"━━━━━━━━━━━━━━━━━━━━{nl}"
            f"✅ All metrics within targets. No adjustments needed.{nl}"
        )

    # Historical trend (last 7 days)
    if len(config_history.get("snapshots", [])) > 1:
        m += f"{nl}<b>📅 7-Day Trend</b>{nl}"
        m += f"━━━━━━━━━━━━━━━━━━━━{nl}"
        snaps = config_history["snapshots"][-7:]
        wr_trend = [s["win_rate"] for s in snaps]
        lr_trend = [s["loss_rate"] for s in snaps]
        m += f"Win rate:  {wr_trend[0]:.1f}% → {wr_trend[-1]:.1f}% ({'📈' if wr_trend[-1] > wr_trend[0] else '📉'}){nl}"
        m += f"Loss rate: {lr_trend[0]:.1f}% → {lr_trend[-1]:.1f}% ({'📉' if lr_trend[-1] < lr_trend[0] else '📈'}){nl}"

    return m

# ═══════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════
def main():
    signals = load_signals()
    config_history = load_json(CONFIG_LOG, {"snapshots": [], "adjustments": []})

    stats = compute_stats(signals)
    if not stats:
        print("No closed signals yet")
        return

    # Get previous snapshot for comparison
    prev_stats = config_history["snapshots"][-1] if config_history["snapshots"] else None

    # Auto-adjust
    severity, adjustments = auto_adjust(stats, config_history)

    # Save snapshot
    config_history["snapshots"].append(stats)
    if len(config_history["snapshots"]) > 30:
        config_history["snapshots"] = config_history["snapshots"][-30:]

    # Save adjustments log
    if adjustments:
        config_history["adjustments"].append({
            "timestamp": stats["timestamp"],
            "severity": severity,
            "adjustments": adjustments
        })
    save_json(CONFIG_LOG, config_history)

    # Build and send report
    report = build_report(stats, prev_stats, severity, adjustments, config_history)
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

    print(f"Daily report sent. Severity: {severity}")
    print(f"WR={stats['win_rate']}% LR={stats['loss_rate']}% Expectancy=${stats['expectancy']}")

if __name__ == "__main__":
    main()
