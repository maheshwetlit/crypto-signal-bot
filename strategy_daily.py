"""
strategy_daily.py — Hermes v12 "daily dip-in-trend" (single source of truth).

Used by both backtest_daily.py and the live paper bot (crypto_signal_bot.py),
so what is tested is exactly what runs.

Rules (fixed BEFORE any test; change only with a fresh backtest):
  Universe   Each day: perps with >= 120 daily bars, ranked by 30-day average
             dollar volume; trade only the top 30 (point-in-time, no look-ahead).
  Long       close > SMA100 (uptrend) AND 20-day return > 0 (strength)
             AND RSI(3) < 20 (short-term dip).
  Short      mirror: close < SMA100, 20-day return < 0, RSI(3) > 80.
  Entry      Limit order for the next day at close -/+ 0.25 x ATR(14).
             Valid one day. Fills only if price trades through it.
  Stop       2.5 x ATR(14) beyond the limit price (exchange stop, market fill).
  Exit       "Bounce": close > highest close of the previous 5 days (long) /
             close < lowest close of previous 5 days (short) -> exit next open.
             Time stop: 10 daily closes after fill.
  Sizing     Risk 1% of equity per trade (stop distance). Total notional <= 3x
             equity. Slots: start 4, win +1 / loss -1, kept within 3..6.
  Costs      Entry maker fee, exit taker fee + slippage, funding charged every
             day on both sides (conservative: we assume we always PAY funding).
  Ambiguity  If one bar touches both the fill and the stop, it counts as stopped.
"""
import numpy as np
import pandas as pd

VERSION = "v12-daily-dip"
DAY_MS = 86_400_000


class D:
    TOP_N_VOLUME = 30
    VOL_LOOKBACK = 30
    MIN_HISTORY = 120
    TREND_SMA = 100
    STRENGTH_LOOKBACK = 20
    RSI_N = 3
    RSI_LONG = 20
    RSI_SHORT = 80
    ATR_N = 14
    LIMIT_OFFSET_ATR = 0.25
    STOP_ATR = 2.5
    EXIT_LOOKBACK = 5
    TIME_STOP_DAYS = 10
    ALLOW_SHORTS = True

    RISK_PCT = 1.0
    MAX_LEVERAGE = 3.0
    MAX_POS_NOTIONAL_X = 1.0   # one position <= 1x equity
    SLOTS_START = 4
    SLOTS_MIN = 3
    SLOTS_MAX = 6

    MAKER_FEE_PCT = 0.015      # Hyperliquid base tier
    TAKER_FEE_PCT = 0.045
    SLIPPAGE_PCT = 0.05        # on market exits / stops
    FUNDING_PCT_PER_DAY = 0.03 # paid every day held, both sides (conservative)
    START_EQUITY = 1000.0


# ───────────────────────── indicators ─────────────────────────
def rsi(s, n):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df, n):
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def features(df):
    """Per-coin daily features. Row i uses only data up to and including day i's close."""
    f = df.copy()
    f["sma"] = f["close"].rolling(D.TREND_SMA).mean()
    f["ret20"] = f["close"] / f["close"].shift(D.STRENGTH_LOOKBACK) - 1
    f["rsi"] = rsi(f["close"], D.RSI_N)
    f["atr"] = atr(f, D.ATR_N)
    f["dv30"] = (f["close"] * f["volume"]).rolling(D.VOL_LOOKBACK).mean()
    f["hh"] = f["close"].rolling(D.EXIT_LOOKBACK).max().shift(1)
    f["ll"] = f["close"].rolling(D.EXIT_LOOKBACK).min().shift(1)
    f["bars"] = np.arange(1, len(f) + 1)
    f["long_setup"] = (f["close"] > f["sma"]) & (f["ret20"] > 0) & (f["rsi"] < D.RSI_LONG)
    f["short_setup"] = (f["close"] < f["sma"]) & (f["ret20"] < 0) & (f["rsi"] > D.RSI_SHORT)
    if not D.ALLOW_SHORTS:
        f["short_setup"] = False
    ok = f["bars"] >= D.MIN_HISTORY
    f["long_setup"] &= ok & f["sma"].notna()
    f["short_setup"] &= ok & f["sma"].notna()
    return f


def rank_universe(day_rows):
    """day_rows: DataFrame (one row per coin for a given day) with dv30, bars.
    Returns set of coins in the tradeable universe that day."""
    r = day_rows[(day_rows["bars"] >= D.MIN_HISTORY) & day_rows["dv30"].notna()]
    return set(r.sort_values("dv30", ascending=False).head(D.TOP_N_VOLUME)["coin"])


def make_order(coin, side, row):
    """Limit order for the next day from a signal row (day close)."""
    a = float(row["atr"])
    c = float(row["close"])
    if not np.isfinite(a) or a <= 0:
        return None
    if side == "LONG":
        limit = c - D.LIMIT_OFFSET_ATR * a
        stop = limit - D.STOP_ATR * a
    else:
        limit = c + D.LIMIT_OFFSET_ATR * a
        stop = limit + D.STOP_ATR * a
    if limit <= 0 or stop <= 0:
        return None
    return {"coin": coin, "side": side, "limit": limit, "stop": stop,
            "stop_pct": abs(limit - stop) / limit * 100}


def size_notional(equity, stop_pct, used_notional):
    """Risk-based size, capped by per-position and total leverage limits."""
    n = equity * D.RISK_PCT / stop_pct
    n = min(n, equity * D.MAX_POS_NOTIONAL_X, equity * D.MAX_LEVERAGE - used_notional)
    return max(0.0, n)


# ───────────────────────── fills & exits on one bar ─────────────────────────
def try_fill(order, bar):
    """Limit fill on a bar (any timeframe). Returns fill price or None."""
    if order["side"] == "LONG":
        if bar["low"] <= order["limit"]:
            return min(bar["open"], order["limit"])
    else:
        if bar["high"] >= order["limit"]:
            return max(bar["open"], order["limit"])
    return None


def stop_hit(pos, bar, fill_bar=False):
    """Returns stop exit price or None. On the fill bar, open is not used."""
    if pos["side"] == "LONG":
        if bar["low"] <= pos["stop"]:
            return pos["stop"] if fill_bar else min(bar["open"], pos["stop"])
    else:
        if bar["high"] >= pos["stop"]:
            return pos["stop"] if fill_bar else max(bar["open"], pos["stop"])
    return None


def exit_signal(pos, row, days_held):
    """Daily-close exit check. Returns reason or None."""
    if pos["side"] == "LONG" and row["close"] > row["hh"]:
        return "BOUNCE"
    if pos["side"] == "SHORT" and row["close"] < row["ll"]:
        return "BOUNCE"
    if days_held >= D.TIME_STOP_DAYS:
        return "TIME"
    return None


def close_pnl(pos, exit_px, days_held):
    """Net P&L in USD after fees, slippage and funding."""
    n, e, side = pos["notional"], pos["entry"], pos["side"]
    gross = n * ((exit_px - e) / e if side == "LONG" else (e - exit_px) / e)
    exit_notional = n * exit_px / e
    fees = n * D.MAKER_FEE_PCT / 100 + exit_notional * (D.TAKER_FEE_PCT + D.SLIPPAGE_PCT) / 100
    funding = n * D.FUNDING_PCT_PER_DAY / 100 * max(days_held, 1)
    net = gross - fees - funding
    return {"gross_usd": gross, "cost_usd": fees + funding, "net_usd": net,
            "net_pct": net / n * 100, "r": net / (n * pos["stop_pct"] / 100)}


def next_slots(slots, won):
    return min(D.SLOTS_MAX, slots + 1) if won else max(D.SLOTS_MIN, slots - 1)
