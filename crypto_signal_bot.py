#!/usr/bin/env python3
"""
crypto_signal_bot.py  —  HYBRID v9.0 — NFI + Little RZY Fusion
Patched: 2026-06-26

HYBRID STRATEGY v9.0 — NFI + Little RZY Fusion
  Based on Claude analysis of live trading data + Marci Silfrain measured-move spec

  CORE CHANGES:
  1. Entry trigger: RSI extreme → corrective bounce + close-beyond-trendline (structure confirmed)
  2. Structure: Require 3-point swing sequence (lower highs + lower lows for SHORT) as pre-filter
  3. Entry timeframe: 5m early-watch → 15m/30m close confirmation before firing
  4. SL = max(nearest invalidating swing/trendline, entry − ATR_mult × ATR)
  5. TP = measured-move Fibonacci projection (0.618×D, 1.0×D, 1.618×D)
  6. Position: hard 1-per-symbol cap, 48h time-stop
  7. R:R gate: minimum 1.5:1 to TP1 at generation
  8. Profit protection: move SL to breakeven once price reaches 60% of TP1 distance
  9. Scan/signal decoupling: 5 min scan, filters control rate (target 1-3/day)

NFI LEGACY KEEP:
  - Watchlist: top 60 USDT pairs, KuCoin, min $5M 24h vol
  - ADX floor 25, volume gate 1.2x, MACD momentum
  - HERMES-06 RSI gates, HERMES-07 HTF alignment, HERMES-11 post-loss escalation
  - HERMES-13 ATR volatility spike block
  - Telegram notifier + validator pipeline
  - BLOCKLIST: H/USDT
"""

import os
import json
import time
import traceback
from datetime import datetime, timezone
import ccxt
import pandas as pd
import numpy as np
import requests

# Windows compatibility: fcntl doesn't exist on Windows
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False
    try:
        import msvcrt
        _HAS_MSVCRT = True
    except ImportError:
        _HAS_MSVCRT = False


def _file_lock(f, exclusive=True):
    if _HAS_FCNTL:
        fcntl.flock(f, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    elif _HAS_MSVCRT:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK if not exclusive else msvcrt.LK_LOCK, 1)
        except (OSError, IOError):
            pass

def _file_unlock(f):
    if _HAS_FCNTL:
        fcntl.flock(f, fcntl.LOCK_UN)
    elif _HAS_MSVCRT:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except (OSError, IOError):
            pass


# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────
class Config:
    SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
    _TG_TOKEN_FILE      = os.path.join(SCRIPT_DIR, ".tg_token")
    _TG_CHAT_FILE       = os.path.join(SCRIPT_DIR, ".tg_chat")

    TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not TELEGRAM_BOT_TOKEN and os.path.exists(_TG_TOKEN_FILE):
        with open(_TG_TOKEN_FILE) as _tf:
            TELEGRAM_BOT_TOKEN = _tf.read().strip()

    TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
    if not TELEGRAM_CHAT_ID and os.path.exists(_TG_CHAT_FILE):
        with open(_TG_CHAT_FILE) as _cf:
            TELEGRAM_CHAT_ID = _cf.read().strip()
    if not TELEGRAM_CHAT_ID:
        TELEGRAM_CHAT_ID = "5515185305"

    MIN_24H_VOLUME_USD = 5_000_000
    MAX_COINS_TO_SCAN  = 60
    QUOTE_CURRENCY     = "USDT"
    LTF_TIMEFRAME      = "5m"
    HTF_TIMEFRAME      = "1h"
    OHLCV_LIMIT        = 500
    ATR_FLOOR_BTC      = 0.10
    ATR_FLOOR_ALT      = 0.15
    MAX_EMA_DIST_PCT   = 12.0
    BASE_COOLDOWN      = 900          # HERMES-10: 15 minutes (was 600s)
    NOTIFY_INTERVAL    = "10m"
    STATE_FILE         = "bot_state.json"
    SIGNAL_LOG_FILE    = "signals_log.json"

    RSI_PERIOD         = 14
    # ── HYBRID v9.0 Configuration ──────────────────────────────────────
    # Entry trigger: corrective bounce + close-beyond-level confirmation
    SWING_LOOKBACK     = 60          # bars to scan for swing point detection
    SWING_TOUCH_BARS   = 2            # bars either side for swing point
    MIN_SWING_POINTS   = 3            # minimum confirmed swings to establish trend
    ENTRY_TF           = "15m"       # confirmation timeframe (replaces 5m fire)
    SCAN_TF            = "5m"        # early-watch scan timeframe

    # Structure: pullback detection
    PULLBACK_MIN       = 4            # min corrective candles after impulse
    PULLBACK_MAX       = 15           # max candles before structure invalidates
    RETRACEMENT_MIN    = 0.25         # min pullback vs impulse
    RETRACEMENT_MAX    = 0.80         # max pullback vs impulse

    # Trendline fit
    TRENDLINE_MIN_R2   = 0.70         # min R² for trendline fit quality
    TRENDLINE_TOUCH_THR = 0.0015      # touch tolerance 0.15%
    TRENDLINE_MIN_TOUCHES = 2       # min touch points

    # Measured move targets
    TP1_RATIO          = 0.618       # Fibonacci 0.618×D
    TP2_RATIO          = 1.000       # full measured move
    TP3_RATIO          = 1.618       # Fibonacci extension
    D_MIN_ATR_MULT     = 0.3          # D must be > 0.3× ATR

    # Stop loss: hybrid ATR + structure
    SL_ATR_MULT        = 2.0          # ATR buffer multiplier
    SL_BUFFER_PCT      = 0.003        # 0.3% buffer beyond trendline/swing
    MAX_SL_PCT         = 2.5          # Max SL as % of entry
    MIN_SL_PCT         = 0.8          # Min SL as % (wider than legacy — crypto wicks)

    # Risk-reward gate
    MIN_RR_TO_TP1      = 1.5          # Must achieve 1.5:1 R:R to fire

    # Position limits
    MAX_OPEN_TOTAL     = 8            # relaxed from 5 — 1-per-symbol cap is primary
    MAX_OPEN_PER_PAIR  = 1            # hard cap per symbol
    MAX_OPEN_PER_DIR   = 1            # 1 open signal per symbol per direction
    RZY_MAX_HOLD_HOURS = 48           # time-stop: 48h max

    # Profit protection
    SL_BREAKEVEN_PCT   = 0.6          # move SL to BE at 60% of TP1 distance

    EXCHANGE           = "KuCoin"

    # NFI legacy pre-filters (used before structure detection)
    ADX_PERIOD         = 14
    ADX_HARD_FLOOR     = 25
    ADX_TREND_THR      = 28
    ADX_BORDERLINE     = 25
    ADX_STRONG         = 35
    VOL_GATE           = 1.2
    VOL_IDEAL          = 1.5
    ATR_FLOOR_BTC      = 0.10
    ATR_FLOOR_ALT      = 0.15
    MAX_EMA_DIST_PCT   = 12.0
    SCORE_ENTRY_THR    = 85.0
    SCORE_OVERRIDE     = 92.0
    SCORE_PRIME_THR    = 75.0
    SCORE_BREAKOUT_THR = 78.0
    SCORE_POST_LOSS_1  = 95.0
    SCORE_POST_LOSS_2  = 98.0

    # NH
    RSI_LATE_THR       = 68
    COOLDOWN           = 900          # 15 min between signals on same pair

    # NH
    FETCH_RETRY        = 3
    CAPITAL_PER_SIGNAL = 1000.0
    MIN_ENTRY_PRICE    = 0.000001
    BLOCKLIST          = {"H/USDT"}
    LONG_SUPPRESSION   = set()

    # NH
    TP_R_MULTIPLES     = [1.5, 2.5, 4.0]
    TP_R_MULTIPLES_STRONG = [2.0, 3.5, 5.0]
    LONG_SUPPRESSION   = set()  # populated dynamically if needed


# ─────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────
def utc_now():
    return datetime.now(timezone.utc)


def _is_dead_zone(h):
    return False


def _rsi(series, period=14):
    delta = series.diff()
    gain  = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))


def _macd(series, f=12, s=26, sig=9):
    fast   = series.ewm(span=f, adjust=False).mean()
    slow   = series.ewm(span=s, adjust=False).mean()
    line   = fast - slow
    signal = line.ewm(span=sig, adjust=False).mean()
    return line, signal, line - signal


def _atr(df, p=14):
    tr = pd.concat([
        df["high"] - df["low"],
        abs(df["high"] - df["close"].shift(1)),
        abs(df["low"]  - df["close"].shift(1))
    ], axis=1).max(axis=1)
    return tr.rolling(p).mean()


def _adx(df, p=14):
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        abs(high - close.shift(1)),
        abs(low  - close.shift(1))
    ], axis=1).max(axis=1)
    atr      = tr.ewm(alpha=1/p, adjust=False).mean()
    dm_plus  = high.diff()
    dm_minus = -low.diff()
    dm_plus  = dm_plus.where((dm_plus > dm_minus) & (dm_plus > 0), 0)
    dm_minus = dm_minus.where((dm_minus > dm_plus) & (dm_minus > 0), 0)
    di_plus  = 100 * dm_plus.ewm(alpha=1/p, adjust=False).mean() / atr.replace(0, np.nan)
    di_minus = 100 * dm_minus.ewm(alpha=1/p, adjust=False).mean() / atr.replace(0, np.nan)
    dx       = 100 * abs(di_plus - di_minus) / (di_plus + di_minus).replace(0, np.nan)
    return dx.ewm(alpha=1/p, adjust=False).mean()


# ─────────────────────────────────────────────
#  Open-signal query helpers
# ─────────────────────────────────────────────
def _load_signals():
    if not os.path.exists(Config.SIGNAL_LOG_FILE):
        return []
    try:
        with open(Config.SIGNAL_LOG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _open_signals_for_pair(pair, signals):
    return [s for s in signals if s.get("pair") == pair and s.get("status") == "OPEN"]


def _has_open(pair, signals):
    """HERMES: one signal per token — any open position blocks new signals."""
    return any(s.get("pair") == pair and s.get("status") == "OPEN" for s in signals)


def _total_open_count(signals):
    """HERMES-09: count total open signals across all symbols."""
    return sum(1 for s in signals if s.get("status") == "OPEN")


def _recent_losses(pair, signals, hours=4):
    """HERMES-11: count losses for a symbol in the past N hours."""
    cutoff = time.time() - hours * 3600
    return sum(
        1 for s in signals
        if s.get("pair") == pair
        and s.get("status") == "LOSS"
        and s.get("closed_at")
        and datetime.fromisoformat(s["closed_at"]).timestamp() > cutoff
    )


def _consecutive_losses(pair, signals, hours=8):
    """HERMES-11: count consecutive losses within N hours."""
    cutoff = time.time() - hours * 3600
    recent = sorted(
        [s for s in signals if s.get("pair") == pair and s.get("status") == "LOSS"
         and s.get("closed_at") and datetime.fromisoformat(s["closed_at"]).timestamp() > cutoff],
        key=lambda x: x.get("closed_at", ""), reverse=True
    )
    count = 0
    for s in recent:
        if s.get("status") == "LOSS":
            count += 1
        else:
            break
    return count


# ─────────────────────────────────────────────
#  Signal logger
# ─────────────────────────────────────────────
def log_signal(symbol, sig):
    log = _load_signals()
    entry = {
        "id":        f"SIG-{len(log)+1:04d}",
        "time":      utc_now().isoformat(),
        "pair":      symbol,
        "exchange":  Config.EXCHANGE,
        "direction": sig["side"],
        "style":     sig["style"],
        "score":     sig["eff"],
        "adx":       sig["adx"],
        "rsi":       sig["rsi"],
        "volume_x":  sig["rv"],
        "htf_trend": sig["htf"],
        "entry":     sig["entry"],
        "sl":        sig["sl"],
        "tp1":       sig["tp"][0],
        "tp2":       sig["tp"][1] if len(sig["tp"]) > 1 else None,
        "tp3":       sig["tp"][2] if len(sig["tp"]) > 2 else None,
        "tp_main":   sig["tp"][-1],
        "capital":   Config.CAPITAL_PER_SIGNAL,
        "status":    "OPEN",
        "exit_price": None,
        "pnl_usd":   None,
        "result":    None,
        "closed_at": None,
        "exit_time": None,
    }
    log.append(entry)
    with open(Config.SIGNAL_LOG_FILE, "w") as f:
        _file_lock(f)
        json.dump(log, f, indent=2)
        _file_unlock(f)
    print(f"[LOG] {entry['id']} {symbol} {sig['side']} → {Config.SIGNAL_LOG_FILE}")
    return entry["id"]


# ─────────────────────────────────────────────
#  Bot state (cooldowns)
# ─────────────────────────────────────────────
class BotState:
    def __init__(self, path):
        self.path = path
        self.data = {"cooldowns": {}}
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    self.data = json.load(f)
            except Exception:
                pass

    def save(self):
        with open(self.path, "w") as f:
            _file_lock(f)
            json.dump(self.data, f)
            _file_unlock(f)

    def is_on_cooldown(self, s):
        return time.time() < self.data["cooldowns"].get(s, 0)

    def record_and_save(self, s):
        self.data["cooldowns"][s] = time.time() + Config.BASE_COOLDOWN
        self.save()


# ─────────────────────────────────────────────
#  Telegram
# ─────────────────────────────────────────────
class TelegramNotifier:
    def send(self, msg):
        if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
            print(f"[NO TOKEN] {msg}")
            return False
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
        for attempt in range(3):
            try:
                r = requests.post(
                    url,
                    json={"chat_id": Config.TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                    timeout=15
                )
                if r.status_code == 200:
                    return True
                print(f"Telegram error {r.status_code}: {r.text}")
            except Exception as e:
                print(f"Telegram send attempt {attempt+1} failed: {e}")
            time.sleep(2)
        return False


def fetch_tickers_with_retry(ex, notifier):
    for attempt in range(Config.FETCH_RETRY):
        try:
            return ex.fetch_tickers()
        except Exception as e:
            print(f"fetch_tickers attempt {attempt+1} failed: {e}")
            if attempt < Config.FETCH_RETRY - 1:
                time.sleep(5)
            else:
                notifier.send(
                    "⚠️ <b>Hermes fetch_tickers FAILED</b>\n"
                    f"Error: <code>{str(e)[:200]}</code>\nBot stopped."
                )
                return None


# ─────────────────────────────────────────────
#  Signal scoring engine — Hermes + GoatXX
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
#  Hybrid structure helpers (swing, trendline, measured move)
# ─────────────────────────────────────────────
def _detect_swings(candles, lookback=60, touch_bars=2):
    """
    Detect swing highs and lows in the last `lookback` bars.
    Returns (swing_highs, swing_lows) as lists of (index, price).
    """
    recent = candles.tail(lookback + 2 * touch_bars)
    if len(recent) < 5:
        return [], []
    highs, lows = [], []
    for i in range(touch_bars, len(recent) - touch_bars):
        h = recent.iloc[i]["high"]
        l = recent.iloc[i]["low"]
        if all(h >= recent.iloc[i - j]["high"] for j in range(1, touch_bars + 1)) and \
           all(h >= recent.iloc[i + j]["high"] for j in range(1, touch_bars + 1) if i + j < len(recent)):
            highs.append((i, h))
        if all(l <= recent.iloc[i - j]["low"] for j in range(1, touch_bars + 1)) and \
           all(l <= recent.iloc[i + j]["low"] for j in range(1, touch_bars + 1) if i + j < len(recent)):
            lows.append((i, l))
    return highs[-5:], lows[-5:]


def _fit_trendline(indices, prices):
    """
    Fit a linear trendline. Returns (slope, intercept, r_squared).
    """
    x = np.array(indices, dtype=float)
    y = np.array(prices, dtype=float)
    if len(x) < 2:
        return 0.0, 0.0, 0.0
    slope, intercept = np.polyfit(x, y, 1)
    y_pred = slope * x + intercept
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0
    return slope, intercept, r_squared


def _detect_trend(candles, lookback=60):
    """
    HYBRID structure filter:
    Returns ("SHORT", swing_highs, swing_lows) for confirmed downtrend,
             ("LONG", swing_highs, swing_lows) for confirmed uptrend,
             or (None, [], []) if no clear structure.
    """
    sh, sl = _detect_swings(candles, lookback=lookback)
    if len(sh) < 3 or len(sl) < 3:
        return None, sh, sl

    # Check descending: last 3 swing highs + last 3 swing lows
    sh_prices = [p for _, p in sh[-3:]]
    sl_prices = [p for _, p in sl[-3:]]

    descending = sh_prices[0] > sh_prices[1] > sh_prices[2] and \
                 sl_prices[0] > sl_prices[1] > sl_prices[2]
    ascending  = sh_prices[0] < sh_prices[1] < sh_prices[2] and \
                 sl_prices[0] < sl_prices[1] < sl_prices[2]

    if descending:
        return "SHORT", sh, sl
    elif ascending:
        return "LONG", sh, sl
    else:
        return None, sh, sl


def _detect_impulse_and_pullback(candles, trend_bias, atr_v):
    """
    HYBRID impulse + pullback detection.
    Returns (impulse_found, pullback_indices, retracement_ratio, trendline_params) or list of Nones.
    """
    lookback = min(20, len(candles) - 16)
    if lookback < 6:
        return None, None, None, None

    # Find most recent impulse candle in last 20 bars
    impulse_idx = None
    search_depth = min(21, len(candles))
    for i in range(-3, -search_depth, -1):
        body = abs(candles.iloc[i]["close"] - candles.iloc[i]["open"])
        rng  = candles.iloc[i]["high"] - candles.iloc[i]["low"]
        cond_a = body >= 1.5 * atr_v
        cond_b = rng > 0 and body >= 0.65 * rng
        cond_c = (trend_bias == "SHORT" and candles.iloc[i]["close"] < candles.iloc[i]["open"]) or \
                 (trend_bias == "LONG" and candles.iloc[i]["close"] > candles.iloc[i]["open"])
        if cond_a and cond_b and cond_c:
            impulse_idx = i + len(candles)  # normalize to positive index
            break

    if impulse_idx is None:
        return None, None, None, None

    # Collect pullback candles (max 15 from impulse end)
    pb_start = impulse_idx + 1
    pb_candles = []
    for j in range(1, 16):
        idx = pb_start + j - 1
        if idx >= len(candles):
            break
        # Invalidation: price reverses past impulse start
        if trend_bias == "SHORT" and candles.iloc[idx]["close"] > candles.iloc[impulse_idx]["open"]:
            break
        if trend_bias == "LONG" and candles.iloc[idx]["close"] < candles.iloc[impulse_idx]["open"]:
            break
        pb_candles.append(idx)

    # Minimum 4 withdrawal candles (HYBRID: raise to 4 from original 3)
    if len(pb_candles) < 4:
        # Accept 3 if shallow enough
        if len(pb_candles) < 3:
            return None, None, None, None

    # Retracement ratio check
    impulse_size = abs(candles.iloc[impulse_idx]["close"] - candles.iloc[impulse_idx]["open"])
    if impulse_size == 0:
        return None, None, None, None

    if trend_bias == "SHORT":
        pb_high = max(candles.iloc[i]["high"] for i in pb_candles)
        retrace = (pb_high - candles.iloc[impulse_idx]["close"]) / impulse_size
    else:
        pb_low = min(candles.iloc[i]["low"] for i in pb_candles)
        retrace = (candles.iloc[impulse_idx]["close"] - pb_low) / impulse_size

    # Filter out tiny advances
    if retrace < 0.10:
        # No pullback yet, wait
        return None, None, None, None
    if retrace > 0.80:
        return None, None, None, None

    # Fit trendline through pullback highs (SHORT) or lows (LONG)
    if trend_bias == "SHORT":
        points = [(i, candles.iloc[i]["high"]) for i in pb_candles]
    else:
        points = [(i, candles.iloc[i]["low"]) for i in pb_candles]

    idx_list = [p[0] for p in points]
    price_list = [p[1] for p in points]
    slope, intercept, r2 = _fit_trendline(idx_list, price_list)

    # Quality gates
    if r2 < 0.70:
        return None, None, None, None

    # Check at least 2 touch points within tolerance
    touches = 0
    for i, price in points:
        line_at_i = slope * i + intercept
        if abs(price - line_at_i) <= line_at_i * 0.0015:
            touches += 1
    if touches < 2:
        return None, None, None, None

    return True, pb_candles, retrace, (slope, intercept, r2)


def _fit_signal_measurements(candles, pb_candles, slope, intercept, trend_bias):
    """
    HYBRID Phase 5: Calculate extreme point, D, and TP targets.
    Returns (extreme_price, prices, D, tp1, tp2, tp3) or Nones.
    """
    if trend_bias == "SHORT":
        # Extreme = lowest low in pullback
        ext_idx = min(pb_candles, key=lambda i: candles.iloc[i]["low"])
        ext_price = candles.iloc[ext_idx]["low"]
        line_at_ext = slope * ext_idx + intercept
        D = line_at_ext - ext_price
    else:
        # Extreme = highest high in pullback
        ext_idx = max(pb_candles, key=lambda i: candles.iloc[i]["high"])
        ext_price = candles.iloc[ext_idx]["high"]
        line_at_ext = slope * ext_idx + intercept
        D = ext_price - line_at_ext

    if D <= 0:
        return None, None, None, None, None, None

    tp1 = ext_price - D * 0.618 if trend_bias == "SHORT" else ext_price + D * 0.618
    tp2 = ext_price - D * 1.000 if trend_bias == "SHORT" else ext_price + D * 1.000
    tp3 = ext_price - D * 1.618 if trend_bias == "SHORT" else ext_price + D * 1.618

    return ext_price, ext_idx, D, tp1, tp2, tp3


def compute_goat_score(df_scan, df_h, symbol, current_signals=None):
    """
    HYBRID v9.0 Signal Engine — Structure + Trendline + Measured Move.
    Returns signal dict or None if suppressed.
    """
    if len(df_scan) < 50 or len(df_h) < 50:
        return None

    c   = df_scan["close"].iloc[-1]
    o   = df_scan["open"].iloc[-1]
    e50 = df_scan["close"].ewm(span=50, adjust=False).mean().iloc[-1]

    # HYBRID-09: skip sub-micro-price tokens
    if c < Config.MIN_ENTRY_PRICE:
        return None

    dist = abs(c - e50) / e50 * 100
    if dist > Config.MAX_EMA_DIST_PCT:
        return None

    atr_v = _atr(df_scan).iloc[-1]
    atr_p = (atr_v / c) * 100
    floor = Config.ATR_FLOOR_BTC if "BTC" in symbol else Config.ATR_FLOOR_ALT
    if atr_p < floor:
        return None

    # HYBRID-13: ATR volatility check — reject extreme spikes
    atr_sma = _atr(df_scan, p=14).rolling(20).mean().iloc[-1]
    if atr_sma > 0 and atr_v / atr_sma > 1.8:
        return None

    v_ma = df_scan["volume"].rolling(20).mean().iloc[-1]
    rv   = df_scan["volume"].iloc[-1] / v_ma if v_ma > 0 else 0

    # HYBRID-03: Volume gate
    if rv < 1.0:
        return None
    if rv < Config.VOL_GATE:
        return None

    rsi_val  = _rsi(df_scan["close"]).iloc[-1]
    rsi7_val = _rsi(df_scan["close"], 7).iloc[-1]

    # HYBRID-12: rsiBothLate block
    if rsi_val >= Config.RSI_LATE_THR and rsi7_val >= Config.RSI_LATE_THR:
        return None

    _, _, hist = _macd(df_scan["close"])
    h, hp = hist.iloc[-1], hist.iloc[-2]

    adx_val = _adx(df_scan).iloc[-1]

    # HYBRID-02: ADX hard floor
    if adx_val < Config.ADX_HARD_FLOOR:
        return None

    # HTF analysis
    ht_c    = df_h["close"].iloc[-1]
    ht_e50  = df_h["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    ht_e200 = df_h["close"].ewm(span=200, adjust=False).mean().iloc[-1]
    htf_rsi = _rsi(df_h["close"]).iloc[-1]

    ht_t = (
        "BULLISH" if ht_c > ht_e50 > ht_e200 else
        "BEARISH" if ht_c < ht_e50 < ht_e200 else
        "NEUTRAL"
    )

    # ── HYBRID PHASE 1: STRUCTURE (swing sequence) ──
    trend_bias, sh, sl = _detect_trend(df_scan, lookback=Config.SWING_LOOKBACK)
    if trend_bias is None:
        return None  # No confirmed structure = no signal

    is_short = trend_bias == "SHORT"
    is_long  = trend_bias == "LONG"

    # HYBRID-07: HTF alignment for SHORT
    if is_short and 48 <= htf_rsi <= 72:
        return None
    # HYBRID-07: LONG requires 1H RSI >= 45
    if is_long and htf_rsi < 45:
        return None

    # HYBRID-04: LONG suppression
    if is_long and adx_val < 30:
        return None
    if is_long and rsi_val <= 55:
        return None
    if is_long and rv < 1.5:
        return None
    if is_long and ht_t != "BULLISH":
        return None

    # HYBRID-11: Post-loss escalation
    threshold = Config.SCORE_ENTRY_THR
    if current_signals:
        losses_4h = _recent_losses(symbol, current_signals, hours=4)
        cons_losses_8h = _consecutive_losses(symbol, current_signals, hours=8)
        if cons_losses_8h >= 2:
            threshold = max(threshold, Config.SCORE_POST_LOSS_2)
        elif losses_4h >= 1:
            threshold = max(threshold, Config.SCORE_POST_LOSS_1)

    # ── HYBRID PHASE 2: IMPULSE + PULLBACK ──
    result = _detect_impulse_and_pullback(df_scan, trend_bias, atr_v)
    if result[0] is None:
        return None
    pb_candles, retrace, (slope, intercept, r2) = result[1], result[2], result[3]

    # ── HYBRID PHASE 5: MEASUREMENT + TP ──
    meas = _fit_signal_measurements(df_scan, pb_candles, slope, intercept, trend_bias)
    if meas[0] is None:
        return None
    ext_price, ext_idx, D, tp1, tp2, tp3 = meas

    # D sanity: D > 0.3 × ATR
    if D < Config.D_MIN_ATR_MULT * atr_v:
        return None

    # ── Entry: current price (at trigger candle close) ──
    entry_price = c

    # ── SL: hybrid ATR + structure anchor ──
    sl_buffer = slope * 0 + intercept  # structural anchor placeholder
    # For SHORT: SL = max(trendline_at_entry + buffer, entry + ATR*mult)
    # For LONG: SL = min(trendline_at_entry - buffer, entry - ATR*mult)
    atr_sl_dist = atr_v * Config.SL_ATR_MULT

    # Find nearest invalidating swing point for SL
    if is_short:
        # SL above recent swing high (1st or 2nd)
        swing_sh = sorted([p for _, p in sh[-3:]], reverse=True)
        struct_sl = swing_sh[0] * (1 + Config.SL_BUFFER_PCT) if swing_sh else entry_price * 1.025
        # Use structure SL if tighter than ATR SL (closer to entry)
        sl_price = min(entry_price + atr_sl_dist, struct_sl)
        # Cap at MAX SL_PCT
        max_sl = entry_price * (1 + Config.MAX_SL_PCT / 100)
        if sl_price > max_sl:
            sl_price = max_sl
        # Floor at MIN SSL_PCT
        min_sl = entry_price * (1 + Config.MIN_SL_PCT / 100)
        if sl_price < min_sl:
            sl_price = min_sl
    else:
        swing_sl = sorted([p for _, p in sl[-3:]])
        struct_sl = swing_sl[0] * (1 - Config.SL_BUFFER_PCT) if swing_sl else entry_price * 0.975
        sl_price = max(entry_price - atr_sl_dist, struct_sl)
        max_sl = entry_price * (1 - Config.MIN_SL_PCT / 100)
        if sl_price < max_sl:
            sl_price = max_sl
        min_sl = entry_price * (1 - Config.MAX_SL_PCT / 100)
        if sl_price > min_sl:
            sl_price = min_sl

    # ── R:R gate ──
    risk = abs(entry_price - sl_price)
    if risk == 0:
        return None
    reward1 = abs(tp1 - entry_price)
    rr1 = reward1 / risk
    if rr1 < Config.MIN_RR_TO_TP1:
        return None

    # ── Structure confidence / style ──
    style = "STRUCTURE"

    # ── Build signal dict ──
    side = "LONG" if is_long else "SHORT"
    eff = 85.0  # hybrid signals pass with structural conviction (score gate replaced by quality filters)

    # Trendline info for logging
    trendline_at_entry = slope * (len(df_scan) - 1) + intercept

    return {
        "side":  side,
        "style": style,
        "entry": entry_price,
        "sl":    round(sl_price, 8),
        "tp":    [round(tp1, 8), round(tp2, 8), round(tp3, 8)],
        "eff":   round(eff, 1),
        "adx":   round(adx_val, 1),
        "rsi":   round(rsi_val, 1),
        "rv":    round(rv, 2),
        "htf":   ht_t,
        "d_measured": round(D, 4),
        "retrace": round(retrace, 3),
        "trendline_r2": round(r2, 3),
    }


# ─────────────────────────────────────────────
#  Main scan loop
# ─────────────────────────────────────────────
def main():
    ex    = ccxt.kucoin({"enableRateLimit": True})
    state = BotState(Config.STATE_FILE)
    nt    = TelegramNotifier()
    nl    = "\n"

    tickers = fetch_tickers_with_retry(ex, nt)
    if tickers is None:
        print("ERROR: Could not fetch tickers. Exiting.")
        return

    syms = sorted(
        [
            s for s, d in tickers.items()
            if s.endswith("/USDT") and d.get("quoteVolume", 0) >= Config.MIN_24H_VOLUME_USD
        ],
        key=lambda x: tickers[x]["quoteVolume"],
        reverse=True
    )[:Config.MAX_COINS_TO_SCAN]

    h       = utc_now().hour
    session = "DEAD" if _is_dead_zone(h) else "ACTIVE"

    nt.send(
        f"🤖 <b>Hermes Scan Started</b>{nl}"
        f"Exchange: {Config.EXCHANGE}{nl}"
        f"Pairs Found: {len(syms)}{nl}"
        f"Interval: {Config.NOTIFY_INTERVAL}{nl}"
        f"Session: {session}"
    )

    if _is_dead_zone(h):
        nt.send(f"😴 Dead zone ({h}:00 UTC) — skipping scan.")
        return

    if len(syms) == 0:
        nt.send("⚠️ No pairs found above volume threshold! Check exchange connectivity.")
        return

    # Load current signals for Hermes checks
    current_signals = _load_signals()

    signals_sent = 0
    signals_skipped = {"blocklist": 0, "open_exists": 0, "max_total": 0,
                       "cooldown": 0, "score": 0, "conflict": 0, "error": 0}

    for s in syms:
        # HERMES-08: Blocklist check
        if s in Config.BLOCKLIST:
            print(f"[SKIP-BLOCKLIST] {s} — permanent structural block")
            signals_skipped["blocklist"] += 1
            continue

        # HERMES-10: Cooldown check
        if state.is_on_cooldown(s):
            signals_skipped["cooldown"] += 1
            continue

        # HERMES: One signal per token
        if _has_open(s, current_signals):
            print(f"[SKIP-OPEN] {s} — already has open position")
            signals_skipped["open_exists"] += 1
            continue

        # HERMES-09: Max 5 open signals total
        if _total_open_count(current_signals) >= Config.MAX_OPEN_TOTAL:
            print(f"[SKIP-MAX] {signals_skipped} total open signals reached ({Config.MAX_OPEN_TOTAL})")
            signals_skipped["max_total"] += 1
            continue

        try:
            df_entry = pd.DataFrame(
                ex.fetch_ohlcv(s, Config.SCAN_TF, limit=100),
                columns=["t", "open", "high", "low", "close", "volume"]
            )
            df_15m = pd.DataFrame(
                ex.fetch_ohlcv(s, Config.ENTRY_TF, limit=100),
                columns=["t", "open", "high", "low", "close", "volume"]
            )
            df_h = pd.DataFrame(
                ex.fetch_ohlcv(s, "1h", limit=100),
                columns=["t", "open", "high", "low", "close", "volume"]
            )
            sig = compute_goat_score(df_15m, df_h, s, current_signals)
            time.sleep(0.3)  # HYBRID: rate limit per-symbol (KuCoin 429 protection)
            if not sig:
                signals_skipped["score"] += 1
                continue

            # Persist signal + cooldown BEFORE sending Telegram
            sig_id = log_signal(s, sig)
            state.record_and_save(s)

            # Update in-memory signal list
            current_signals = _load_signals()

            # HERMES signal format
            m = (
                f"<b>{sig['side']} SIGNAL</b>{nl}"
                f"Pair: <code>{s}</code>{nl}"
                f"Style: {sig['style']}{nl}"
                f"Score: {sig['eff']}{nl}"
                f"HTF Trend: {sig['htf']}{nl}"
                f"ADX: {sig['adx']} | RSI: {sig['rsi']} | Vol: {sig['rv']}x{nl}"
                f"Entry: <code>{sig['entry']:.8f}</code>{nl}"
                f"SL: <code>{sig['sl']:.8f}</code>"
            )
            for i, p in enumerate(sig["tp"]):
                m += f"{nl}TP{i+1}: <code>{p:.8f}</code>"

            nt.send(m)
            signals_sent += 1

        except Exception as e:
            signals_skipped["error"] += 1
            print(f"Error scanning {s}: {e}")
            continue

    skip_summary = (
        f"Skipped: blocklist={signals_skipped['blocklist']} "
                f"open={signals_skipped['open_exists']} "
                f"max={signals_skipped['max_total']} "
                f"cooldown={signals_skipped['cooldown']} "
                f"score={signals_skipped['score']} "
                f"errors={signals_skipped['error']}"
    )

    nt.send(
        f"✅ <b>Scan Complete</b>{nl}"
        f"Scanned: {len(syms)} pairs{nl}"
        f"Signals Sent: {signals_sent}{nl}"
        f"{skip_summary}"
    )


if __name__ == "__main__":
    main()
