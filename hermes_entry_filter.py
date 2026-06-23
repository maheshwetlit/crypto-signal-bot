#!/usr/bin/env python3
"""
hermes_entry_filter.py — Signal Entry Filter v1
Filters incoming signals based on historical performance data.

Data-driven rules from 155-signal analysis:
  1. BLOCK LONG signals entirely (15.8% WR, -$2,730 P&L)
  2. BLOCK RSI 50-59 entries (11.1% WR — overbought death zone)
  3. BLOCK entries at 10:00 UTC (9.1% WR — manipulation hour)
  4. REQUIRE volume ≥1.5x (74.6% WR vs 62.5% for 1.5-2.9x)
  5. PREFER RSI 30-49 range (81-85% WR)
  6. PREFER volume ≥3.0x (63.6% WR, but 80% for 5x+)
  7. SCORE <120 preferred (76.3% WR vs 58.1% for 120-139)
  8. BLOCK pairs with >3 losses and negative P&L
  9. CIRCUIT BREAKER: pause new entries after 3+ consecutive losses
  10. MAX 3 concurrent positions (risk management)
"""
import json
import os
from datetime import datetime, timezone
from collections import defaultdict

SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))
SIGNAL_LOG   = os.path.join(SCRIPT_DIR, "signals_log.json")
FILTER_LOG   = os.path.join(SCRIPT_DIR, "hermes_filter_log.json")

# ═══════════════════════════════════════════
#  FILTER RULES (from data analysis)
# ═══════════════════════════════════════════

# Block LONG entirely — 15.8% WR is catastrophic
# NOTE: Only block if historical data confirms poor performance
# The bot's own trend filter (allow_long/allow_short) already prevents counter-trend entries
BLOCK_LONG_SIGNALS = False  # Changed: bot's 8-day MA trend filter is sufficient

# RSI death zones (from data: RSI 50-59 = 11.1% WR)
RSI_BLOCK_RANGES = [(50.0, 59.0)]  # block entries in this RSI range
RSI_PREFERRED_RANGES = [(30.0, 49.0)]  # preferred RSI range (81-85% WR)

# Time-based blocks (from data: 10:00 UTC = 9.1% WR)
BLOCK_HOURS_UTC = [10]  # block entries at these UTC hours
PREFERRED_HOURS_UTC = [5, 6, 8, 9, 13, 20, 23]  # high-WR hours

# Volume requirements (from data: 5x+ = 80% WR, <1.5x = 74.6% but small sample)
MIN_VOLUME_X = 1.5  # minimum volume multiplier
PREFERRED_VOLUME_X = 3.0  # preferred volume multiplier

# Score filter (from data: score 120+ = worse WR than <100)
MAX_SCORE = 139  # block scores above this (58.1% WR for 120-139)
PREFERRED_MAX_SCORE = 119  # preferred max score (70.8% WR)

# Pair blacklist — pairs with >3 losses and negative P&L
PAIR_BLACKLIST = set()  # auto-populated from data

# Circuit breaker
MAX_CONSEC_LOSSES = 3  # pause entries after this many consecutive losses
MAX_CONCURRENT_OPEN = 3  # max concurrent open positions


def load_signals():
    if os.path.exists(SIGNAL_LOG):
        with open(SIGNAL_LOG) as f:
            return json.load(f)
    return []


def load_filter_log():
    if os.path.exists(FILTER_LOG):
        with open(FILTER_LOG) as f:
            return json.load(f)
    return {"blocked_signals": [], "filter_stats": {"total_checked": 0, "passed": 0, "blocked": 0, "reasons": {}}}


def save_filter_log(log):
    with open(FILTER_LOG, "w") as f:
        json.dump(log, f, indent=2)


def get_pair_blacklist(signals):
    """Auto-generate pair blacklist from historical data."""
    pair_stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for s in signals:
        if s.get("status") not in ("WIN", "LOSS"):
            continue
        p = s.get("pair", "?")
        pair_stats[p]["w"] += (1 if s.get("status") == "WIN" else 0)
        pair_stats[p]["l"] += (1 if s.get("status") == "LOSS" else 0)
        pair_stats[p]["pnl"] += s.get("pnl_usd", 0)

    blacklist = set()
    for pair, ps in pair_stats.items():
        # Blacklist if: >3 losses AND negative P&L
        if ps["l"] > 3 and ps["pnl"] < 0:
            blacklist.add(pair)
        # Blacklist if: >5 losses AND win rate <40%
        total = ps["w"] + ps["l"]
        if total > 0 and ps["l"] > 5 and (ps["w"] / total) < 0.4:
            blacklist.add(pair)
    return blacklist


def get_consecutive_losses(signals):
    """Count consecutive losses from most recent closed signals."""
    closed = [s for s in signals if s.get("status") in ("WIN", "LOSS")]
    # Sort by close time, most recent first
    closed.sort(key=lambda s: s.get("closed_at", s.get("exit_time", "")), reverse=True)
    consec = 0
    for s in closed:
        if s.get("status") == "LOSS":
            consec += 1
        else:
            break
    return consec


def get_open_count(signals):
    return len([s for s in signals if s.get("status") == "OPEN"])


def check_entry_filter(signal, signals_log, filter_log=None):
    """
    Check if a new signal should be allowed entry.
    Returns: (allowed: bool, reason: str, confidence: str)
    """
    reasons = []
    confidence = "HIGH"

    # 1. Block LONG signals
    direction = signal.get("direction", signal.get("side", "")).upper()
    if BLOCK_LONG_SIGNALS and direction == "LONG":
        return False, "BLOCKED: LONG signals disabled (15.8% WR)", "NONE"

    # 2. RSI filter
    rsi = signal.get("rsi", 0)
    for rsi_low, rsi_high in RSI_BLOCK_RANGES:
        if rsi_low <= rsi <= rsi_high:
            return False, f"BLOCKED: RSI {rsi} in death zone ({rsi_low}-{rsi_high})", "NONE"

    # 3. Time-of-day filter
    entry_time = signal.get("time", "")
    if entry_time:
        try:
            dt = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
            hour = dt.hour
            if hour in BLOCK_HOURS_UTC:
                return False, f"BLOCKED: Entry at {hour:02d}:00 UTC (low-WR hour)", "NONE"
            if hour not in PREFERRED_HOURS_UTC:
                confidence = "MEDIUM"
                reasons.append(f"Non-preferred hour {hour:02d}:00 UTC")
        except Exception:
            pass

    # 4. Volume filter
    vol = signal.get("volume_x", 0)
    if vol < MIN_VOLUME_X:
        return False, f"BLOCKED: Volume {vol}x < minimum {MIN_VOLUME_X}x", "NONE"
    if vol < PREFERRED_VOLUME_X:
        confidence = "MEDIUM"
        reasons.append(f"Volume {vol}x below preferred {PREFERRED_VOLUME_X}x")

    # 5. Score filter
    score = signal.get("score", 0)
    if score > MAX_SCORE:
        return False, f"BLOCKED: Score {score} > max {MAX_SCORE} (overbought)", "NONE"
    if score > PREFERRED_MAX_SCORE:
        confidence = "MEDIUM"
        reasons.append(f"Score {score} above preferred {PREFERRED_MAX_SCORE}")

    # 6. Pair blacklist
    pair = signal.get("pair", "")
    blacklist = get_pair_blacklist(signals_log)
    if pair in blacklist:
        return False, f"BLOCKED: {pair} is blacklisted (poor historical performance)", "NONE"

    # 7. Circuit breaker — consecutive losses
    consec_losses = get_consecutive_losses(signals_log)
    if consec_losses >= MAX_CONSEC_LOSSES:
        return False, f"BLOCKED: Circuit breaker — {consec_losses} consecutive losses", "NONE"

    # 8. Max concurrent positions
    open_count = get_open_count(signals_log)
    if open_count >= MAX_CONCURRENT_OPEN:
        return False, f"BLOCKED: Max concurrent positions ({open_count}/{MAX_CONCURRENT_OPEN})", "NONE"

    # 9. RSI preferred range bonus
    for rsi_low, rsi_high in RSI_PREFERRED_RANGES:
        if rsi_low <= rsi <= rsi_high:
            confidence = "HIGH"
            reasons.append(f"RSI {rsi} in preferred range ({rsi_low}-{rsi_high})")
            break

    # 10. Volume preferred bonus
    if vol >= 5.0:
        confidence = "HIGH"
        reasons.append(f"High volume {vol}x (80% WR historically)")

    reason_str = " | ".join(reasons) if reasons else "All filters passed"
    return True, reason_str, confidence


def get_filter_summary(signals):
    """Generate a summary of filter performance."""
    blacklist = get_pair_blacklist(signals)
    consec_losses = get_consecutive_losses(signals)
    open_count = get_open_count(signals)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pair_blacklist": sorted(blacklist),
        "consecutive_losses": consec_losses,
        "circuit_breaker_active": consec_losses >= MAX_CONSEC_LOSSES,
        "open_positions": open_count,
        "max_positions": MAX_CONCURRENT_OPEN,
        "long_blocked": BLOCK_LONG_SIGNALS,
        "rules": {
            "block_long": BLOCK_LONG_SIGNALS,
            "rsi_block_ranges": list(RSI_BLOCK_RANGES),
            "block_hours_utc": list(BLOCK_HOURS_UTC),
            "min_volume_x": MIN_VOLUME_X,
            "max_score": MAX_SCORE,
            "max_consec_losses": MAX_CONSEC_LOSSES,
            "max_concurrent_open": MAX_CONCURRENT_OPEN,
        }
    }


if __name__ == "__main__":
    signals = load_signals()
    summary = get_filter_summary(signals)
    print("=" * 60)
    print("ENTRY FILTER STATUS")
    print("=" * 60)
    print(f"Pair blacklist: {summary['pair_blacklist']}")
    print(f"Consecutive losses: {summary['consecutive_losses']} (breaker at {MAX_CONSEC_LOSSES})")
    print(f"Circuit breaker: {'ACTIVE' if summary['circuit_breaker_active'] else 'inactive'}")
    print(f"Open positions: {summary['open_positions']}/{summary['max_positions']}")
    print(f"LONG blocked: {summary['long_blocked']}")
    print(f"RSI block ranges: {RSI_BLOCK_RANGES}")
    print(f"Block hours UTC: {BLOCK_HOURS_UTC}")
    print(f"Min volume: {MIN_VOLUME_X}x")
    print(f"Max score: {MAX_SCORE}")
