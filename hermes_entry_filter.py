#!/usr/bin/env python3
"""
hermes_entry_filter.py — Signal Entry Filter v2 (NFI-Compatible)
Filters incoming signals based on historical performance data.

NFI-compatible rules:
  1. Allow both LONG and SHORT (NFI trades both directions)
  2. Use RSI-3 for NFI signals, RSI-14 for legacy signals
  3. Higher max concurrent positions (NFI uses 6-12)
  4. Volume >= 1.0x for NFI (scalping needs less volume)
  5. No RSI death zone for NFI signals (NFI uses RSI-3 extremes)
  6. No time-of-day blocks for NFI (NFI trades all hours)
  7. Circuit breaker: pause after 5+ consecutive losses (was 3)
  8. Pair blacklist: auto-populated from historical data
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

# Block LONG entirely — DISABLED for NFI (NFI trades both directions)
BLOCK_LONG_SIGNALS = False

# RSI death zones — only apply to legacy signals (not NFI)
# NFI uses RSI-3, not RSI-14, so this filter doesn't apply
RSI_BLOCK_RANGES = []  # disabled — NFI uses RSI-3 extremes
RSI_PREFERRED_RANGES = [(30.0, 49.0)]  # preferred RSI-14 range for legacy

# Time-based blocks — DISABLED for NFI (NFI trades all hours)
BLOCK_HOURS_UTC = []  # disabled
PREFERRED_HOURS_UTC = []  # disabled

# Volume requirements — lowered for NFI scalping
MIN_VOLUME_X = 1.0  # lowered from 1.5 — NFI scalps need less volume
PREFERRED_VOLUME_X = 3.0

# Score filter — NFI scores are 70-100 range
MAX_SCORE = 150  # raised from 139 — NFI scores can be higher
PREFERRED_MAX_SCORE = 119

# Pair blacklist
PAIR_BLACKLIST = set()

# Circuit breaker — more lenient for NFI
MAX_CONSEC_LOSSES = 5  # raised from 3 — NFI grinding needs room
MAX_CONCURRENT_OPEN = 10  # raised from 3 — NFI uses 6-12


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
    NFI signals get different treatment than legacy signals.
    Returns: (allowed: bool, reason: str, confidence: str)
    """
    reasons = []
    confidence = "HIGH"

    # Detect NFI signal
    style = signal.get("style", "")
    is_nfi = style.startswith("NFI_")
    direction = signal.get("direction", signal.get("side", "")).upper()

    # 1. Block LONG signals — only for legacy (not NFI)
    if not is_nfi and BLOCK_LONG_SIGNALS and direction == "LONG":
        return False, "BLOCKED: LONG signals disabled (15.8% WR)", "NONE"

    # 2. RSI filter — only for legacy signals (NFI uses RSI-3)
    if not is_nfi:
        rsi = signal.get("rsi", 0)
        for rsi_low, rsi_high in RSI_BLOCK_RANGES:
            if rsi_low <= rsi <= rsi_high:
                return False, f"BLOCKED: RSI {rsi} in death zone ({rsi_low}-{rsi_high})", "NONE"

    # 3. Time-of-day filter — only for legacy signals
    if not is_nfi:
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

    # 4. Volume filter — lower threshold for NFI
    vol = signal.get("volume_x", 0)
    min_vol = 0.5 if is_nfi else MIN_VOLUME_X  # NFI scalps can work with lower volume
    if vol < min_vol:
        return False, f"BLOCKED: Volume {vol}x < minimum {min_vol}x", "NONE"
    if vol < PREFERRED_VOLUME_X:
        confidence = "MEDIUM"
        reasons.append(f"Volume {vol}x below preferred {PREFERRED_VOLUME_X}x")

    # 5. Score filter — NFI signals already scored 70+
    score = signal.get("score", 0)
    if score > MAX_SCORE:
        return False, f"BLOCKED: Score {score} > max {MAX_SCORE}", "NONE"
    if not is_nfi and score > PREFERRED_MAX_SCORE:
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

    # 9. NFI confidence boost
    if is_nfi:
        confidence = "HIGH"
        reasons.append(f"NFI signal: {style}")

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
