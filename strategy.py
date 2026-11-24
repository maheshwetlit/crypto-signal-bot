"""
strategy.py — Hermes v11 "trend pullback" strategy.

SINGLE SOURCE OF TRUTH: the live bot (crypto_signal_bot.py) and the
backtester (backtest.py) both import this file, so what is backtested is
exactly what trades.

Idea (deliberately simple, few parameters, round numbers):
  * Trade only WITH the 1h trend (EMA50 vs EMA200) and with BTC's 1h trend.
  * Wait for a PULLBACK to the 15m EMA20 instead of chasing a breakout.
  * Enter only when price RESUMES in the trend direction (close beyond the
    previous bar's high/low) on a CLOSED 15m candle.
  * Anti-fake-trend guards: skip over-extended moves (> 1 ATR from EMA20),
    exhausted trends (ADX > 40), climax volume (> 3x), and dead markets.
  * Exits: structural stop (recent swing), target = 2R, breakeven at +1R,
    time stop after 6h. Losses are always >= wins in count terms, but a
    win is worth ~2x a loss, so the strategy needs only ~40% WR + costs.
  * Costs are ALWAYS included: 0.10% fee + 0.05% slippage per side.
  * If a candle touches both stop and target, it is counted as a LOSS.
"""
import numpy as np
import pandas as pd

VERSION = "v11-trend-pullback"


class P:
    """Strategy parameters. Change only after re-running the backtest."""
    TF = "15m"
    TF_MS = 15 * 60 * 1000
    HTF = "1h"
    HTF_MS = 60 * 60 * 1000

    EMA_FAST = 20
    EMA_SLOW = 50
    HTF_EMA_FAST = 50
    HTF_EMA_SLOW = 200
    ATR_N = 14
    RSI_N = 14
    ADX_N = 14
    VOL_MEDIAN_N = 20

    PULLBACK_LOOKBACK = 4     # bars in which price must have touched EMA20 zone
    PULLBACK_TOL_ATR = 0.25   # "touch" = within 0.25 ATR of EMA20
    STRUCTURE_TOL_ATR = 0.5   # pullback must not break EMA50 by > 0.5 ATR
    MAX_EXT_ATR = 1.0         # skip if close is > 1 ATR beyond EMA20 (chasing)
    RSI_LONG = (45, 65)       # momentum present but not overbought
    RSI_SHORT = (35, 55)
    ADX_RANGE = (18, 40)      # trending but not exhausted
    VOL_RANGE = (0.8, 3.0)    # real participation, but no climax spike
    MIN_ATR_PCT = 0.30        # market must move enough to beat costs

    SL_LOOKBACK = 5
    SL_BUFFER_ATR = 0.2
    MIN_RISK_PCT = 0.6
    MAX_RISK_PCT = 3.0
    TP_R = 2.0
    BE_TRIGGER_R = 1.0
    TIME_STOP_BARS = 24       # 24 x 15m = 6h
    COOLDOWN_BARS = 4         # per-pair pause after an exit
    MAX_CHASE_R = 0.5         # live: skip if price ran > 0.5R past signal close

    FEE_PCT = 0.10            # per side (KuCoin spot taker)
    SLIPPAGE_PCT = 0.05       # per side
    USE_BTC_FILTER = True

    @classmethod
    def round_trip_cost(cls):
        return 2 * (cls.FEE_PCT + cls.SLIPPAGE_PCT) / 100.0


# ─────────────────────────── indicators ───────────────────────────
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift(1)).abs(),
                    (df["low"] - df["close"].shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def adx(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    a = tr.ewm(alpha=1 / n, adjust=False).mean()
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    mdm = dn.where((dn > up) & (dn > 0), 0.0)
    pdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / a.replace(0, np.nan)
    mdi = 100 * mdm.ewm(alpha=1 / n, adjust=False).mean() / a.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def to_df(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["t", "open", "high", "low", "close", "volume"])
    return df.astype({"t": "int64", "open": float, "high": float, "low": float,
                      "close": float, "volume": float}).drop_duplicates("t").sort_values("t").reset_index(drop=True)


def closed_only(df, tf_ms, now_ms):
    """Drop the candle that is still forming. Signals use CLOSED candles only."""
    return df[df["t"] + tf_ms <= now_ms].reset_index(drop=True)


def _htf_trend(df_htf):
    h = df_htf.copy()
    ef, es = ema(h["close"], P.HTF_EMA_FAST), ema(h["close"], P.HTF_EMA_SLOW)
    h["up"] = (h["close"] > ef) & (ef > es)
    h["down"] = (h["close"] < ef) & (ef < es)
    h["close_time"] = h["t"] + P.HTF_MS
    # need enough history for EMA200 to mean something
    h.loc[h.index < P.HTF_EMA_SLOW, ["up", "down"]] = False
    return h[["close_time", "up", "down"]]


def _btc_trend(df_btc):
    b = df_btc.copy()
    ef = ema(b["close"], P.HTF_EMA_FAST)
    b["btc_up"] = b["close"] > ef
    b["btc_down"] = b["close"] < ef
    b["close_time"] = b["t"] + P.HTF_MS
    return b[["close_time", "btc_up", "btc_down"]]


# ─────────────────────────── signals ───────────────────────────
def compute_signals(df15, df1h, btc1h=None):
    """Vectorised signal computation on CLOSED candles.
    Row i's signal uses only information available at the close of bar i
    (1h data is joined as-of its own close time, so no look-ahead)."""
    d = df15.copy()
    d["close_time"] = d["t"] + P.TF_MS
    d["ema_f"] = ema(d["close"], P.EMA_FAST)
    d["ema_s"] = ema(d["close"], P.EMA_SLOW)
    d["atr"] = atr(d, P.ATR_N)
    d["rsi"] = rsi(d["close"], P.RSI_N)
    d["adx"] = adx(d, P.ADX_N)
    d["volr"] = d["volume"] / d["volume"].rolling(P.VOL_MEDIAN_N).median().shift(1).replace(0, np.nan)
    d["atr_pct"] = d["atr"] / d["close"] * 100
    d["ext_atr"] = (d["close"] - d["ema_f"]) / d["atr"]

    h = _htf_trend(df1h)
    d = pd.merge_asof(d.sort_values("close_time"), h.sort_values("close_time"),
                      on="close_time", direction="backward")
    if btc1h is not None and P.USE_BTC_FILTER:
        b = _btc_trend(btc1h)
        d = pd.merge_asof(d, b.sort_values("close_time"), on="close_time", direction="backward")
    else:
        d["btc_up"] = True
        d["btc_down"] = True
    for col in ("up", "down", "btc_up", "btc_down"):
        d[col] = d[col].astype("boolean").fillna(False).astype(bool)

    L = P.PULLBACK_LOOKBACK
    lo_L = d["low"].rolling(L).min()
    hi_L = d["high"].rolling(L).max()
    common = ((d["adx"] >= P.ADX_RANGE[0]) & (d["adx"] <= P.ADX_RANGE[1]) &
              (d["volr"] >= P.VOL_RANGE[0]) & (d["volr"] <= P.VOL_RANGE[1]) &
              (d["atr_pct"] >= P.MIN_ATR_PCT))

    long_sig = (common & d["up"] & d["btc_up"] &
                (d["ema_f"] > d["ema_s"]) &
                (lo_L <= d["ema_f"] + P.PULLBACK_TOL_ATR * d["atr"]) &
                (lo_L > d["ema_s"] - P.STRUCTURE_TOL_ATR * d["atr"]) &
                (d["close"] > d["high"].shift(1)) & (d["close"] > d["ema_f"]) & (d["close"] > d["open"]) &
                (d["ext_atr"] <= P.MAX_EXT_ATR) &
                (d["rsi"] >= P.RSI_LONG[0]) & (d["rsi"] <= P.RSI_LONG[1]))

    short_sig = (common & d["down"] & d["btc_down"] &
                 (d["ema_f"] < d["ema_s"]) &
                 (hi_L >= d["ema_f"] - P.PULLBACK_TOL_ATR * d["atr"]) &
                 (hi_L < d["ema_s"] + P.STRUCTURE_TOL_ATR * d["atr"]) &
                 (d["close"] < d["low"].shift(1)) & (d["close"] < d["ema_f"]) & (d["close"] < d["open"]) &
                 (-d["ext_atr"] <= P.MAX_EXT_ATR) &
                 (d["rsi"] >= P.RSI_SHORT[0]) & (d["rsi"] <= P.RSI_SHORT[1]))

    warm = np.arange(len(d)) < 200
    d["long_sig"] = long_sig.fillna(False).astype(bool) & ~warm
    d["short_sig"] = short_sig.fillna(False).astype(bool) & ~warm
    d["sl_long"] = d["low"].rolling(P.SL_LOOKBACK).min() - P.SL_BUFFER_ATR * d["atr"]
    d["sl_short"] = d["high"].rolling(P.SL_LOOKBACK).max() + P.SL_BUFFER_ATR * d["atr"]
    return d


# ─────────────────────────── trade construction & exits ───────────────────────────
def build_trade(side, entry, sl_level):
    """Returns dict(entry, sl, tp, risk) or None if the setup is invalid."""
    if entry is None or sl_level is None or not np.isfinite(sl_level) or entry <= 0:
        return None
    risk = (entry - sl_level) if side == "LONG" else (sl_level - entry)
    if risk <= 0:
        return None
    risk_pct = risk / entry * 100
    if risk_pct > P.MAX_RISK_PCT:
        return None
    if risk_pct < P.MIN_RISK_PCT:
        risk = entry * P.MIN_RISK_PCT / 100
    sl = entry - risk if side == "LONG" else entry + risk
    tp = entry + P.TP_R * risk if side == "LONG" else entry - P.TP_R * risk
    return {"side": side, "entry": float(entry), "sl": float(sl), "tp": float(tp), "risk": float(risk)}


def simulate_exit(trade, bars):
    """Walk CLOSED bars from the entry bar onwards. Deterministic & conservative:
    - stop checked before target inside the same bar (both touched => stop)
    - gaps through the stop fill at the bar open
    - breakeven (+cost) armed after +1R, effective from the NEXT bar
    - time stop at the close of bar TIME_STOP_BARS
    Returns None if still open, else dict(exit, reason, bars, ...)."""
    side, entry, tp, risk = trade["side"], trade["entry"], trade["tp"], trade["risk"]
    sl = trade["sl"]
    be_price = entry * (1 + P.round_trip_cost()) if side == "LONG" else entry * (1 - P.round_trip_cost())
    be_armed = False
    o, h, l, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))
    for k in range(len(bars)):
        if side == "LONG":
            if l[k] <= sl:
                px = min(o[k], sl) if k > 0 else sl
                return _close(trade, px, "BE" if be_armed else "SL", k)
            if h[k] >= tp:
                return _close(trade, tp, "TP", k)
            if h[k] >= entry + P.BE_TRIGGER_R * risk and not be_armed:
                be_armed, sl = True, max(sl, be_price)
        else:
            if h[k] >= sl:
                px = max(o[k], sl) if k > 0 else sl
                return _close(trade, px, "BE" if be_armed else "SL", k)
            if l[k] <= tp:
                return _close(trade, tp, "TP", k)
            if l[k] <= entry - P.BE_TRIGGER_R * risk and not be_armed:
                be_armed, sl = True, min(sl, be_price)
        if k + 1 >= P.TIME_STOP_BARS:
            return _close(trade, c[k], "TIME", k)
    return None


def _close(trade, px, reason, k):
    side, entry = trade["side"], trade["entry"]
    gross = (px - entry) / entry if side == "LONG" else (entry - px) / entry
    net = gross - P.round_trip_cost()
    return {"exit": float(px), "reason": reason, "bars": int(k + 1),
            "gross_pct": gross * 100, "net_pct": net * 100,
            "r_net": net * entry / trade["risk"]}
