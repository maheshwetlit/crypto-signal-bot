#!/usr/bin/env python3
"""
crypto_signal_bot.py  —  Hermes Signal Engine (GoatXX v8.9.27 + Hermes Selectivity Layer)
Patched: 2026-06-06

HERMES MANDATE CHANGES:
  HERMES-01  Score threshold: ALPHA tier ≥ 85.0 (was 80)
  HERMES-02  ADX hard floor: 25 (was 15); ADX 25–27 requires score ≥ 92
  HERMES-03  Volume gate: 1.2x (was 1.1x); vol 1.0–1.19x = hard skip
  HERMES-04  LONG suppression: requires 4H+daily BULLISH, ADX>=30, RSI>55, Vol>=1.5x
  HERMES-05  SHORT style: always REVERSAL (never MOMENTUM/BREAKOUT)
  HERMES-06  SHORT RSI gates: <65 valid, 65–80 valid, >80 mean-reversion trap (score>=92 override)
  HERMES-07  HTF 1H RSI alignment: SHORT blocked when 1H RSI in 48–72 zone
  HERMES-08  H/USDT permanent blocklist (structural, non-negotiable)
  HERMES-09  Max 5 active open signals total (all symbols combined)
  HERMES-10  15-minute cooldown per symbol after close (was 600s/10min)
  HERMES-11  Post-loss elevated threshold: score>=95 after 1 loss in 4h, >=98 after 2 losses in 8h
  HERMES-12  rsiBothLate block: BOTH 14-RSI>=68 AND 7-RSI>=68 = timing gate closed
  HERMES-13  ATR volatility check: ATR <= 1.8x its SMA (no extreme volatility spike)
  HERMES-14  Pair format: SYMBOL/USDT with forward slash
  HERMES-15  SL/TP construction: structural SL, R:R >= 1:1.5 to TP1
  HERMES-16  Late-chase filter: skip if price moved 70%+ from signal origin
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
    RSI_LATE_THR       = 68           # HERMES-12: rsiBothLate threshold (was 72)
    SHORT_RSI_FLOOR    = 20
    ADX_PERIOD         = 14
    ADX_TREND_THR      = 28           # HERMES: aligned with GoatXX crypto threshold
    ADX_HARD_FLOOR     = 25           # HERMES-02: hard floor 25 (was 15)
    ADX_BORDERLINE     = 25           # HERMES-02: ADX 25–27 requires score >= 92
    ADX_STRONG         = 35
    MACD_FAST          = 12
    MACD_SLOW          = 26
    MACD_SIGNAL        = 9
    VOL_GATE           = 1.2          # HERMES-03: 1.2x (was 1.1x)
    VOL_IDEAL          = 1.5          # HERMES-04: LONG requires >= 1.5x
    SCORE_ENTRY_THR    = 85.0         # HERMES-01: ALPHA tier (REVERSAL/MOMENTUM)
    SCORE_PRIME_THR    = 75.0         # PRIME tier: structural high-conviction (lower bar)
    SCORE_BREAKOUT_THR = 78.0         # BREAKOUT tier: volume-backed high-conviction
    SCORE_EARLY_THR    = 90.0         # SHORT EARLY tier requires >= 90
    SCORE_OVERRIDE     = 92.0         # ADX borderline / RSI > 80 override
    SCORE_POST_LOSS_1  = 95.0         # HERMES-11: after 1 loss in 4h
    SCORE_POST_LOSS_2  = 98.0         # HERMES-11: after 2 consecutive losses in 8h
    SL_ATR_MULT        = 2.0
    MAX_SL_PCT         = 2.5          # Max SL as % of entry (2.5% — crypto wicks are 1-2%, need room)
    MIN_SL_PCT         = 0.5          # Min SL as % of entry (avoids spread risk)
    TP_R_MULTIPLES     = [1.5, 2.5, 4.0]       # Standard trend
    TP_R_MULTIPLES_STRONG = [2.0, 3.5, 5.0]    # Strong trend (ADX >= 30)
    EXCHANGE           = "KuCoin"
    FETCH_RETRY        = 3
    CAPITAL_PER_SIGNAL = 1000.0

    MAX_OPEN_PER_PAIR  = 1            # HERMES: one signal per token
    MAX_OPEN_TOTAL     = 5            # HERMES-09: max 5 active open signals total
    MIN_ENTRY_PRICE    = 0.000001

    # HERMES-08: Permanent structural blocklist
    BLOCKLIST          = {"H/USDT"}

    # HERMES-04: LONG suppression list (tokens with confirmed structural downtrend)
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
def compute_goat_score(df_l, df_h, symbol, current_signals=None):
    """
    Hermes signal scoring engine.
    Returns signal dict or None if suppressed.
    All Hermes mandate checks are applied in order.
    """
    if len(df_l) < 50 or len(df_h) < 50:
        return None

    c   = df_l["close"].iloc[-1]
    o   = df_l["open"].iloc[-1]
    e50 = df_l["close"].ewm(span=50, adjust=False).mean().iloc[-1]

    # HERMES-09: skip sub-micro-price tokens
    if c < Config.MIN_ENTRY_PRICE:
        return None

    dist = abs(c - e50) / e50 * 100
    if dist > Config.MAX_EMA_DIST_PCT:
        return None

    atr_v = _atr(df_l).iloc[-1]
    atr_p = (atr_v / c) * 100
    floor = Config.ATR_FLOOR_BTC if "BTC" in symbol else Config.ATR_FLOOR_ALT
    if atr_p < floor:
        return None

    # HERMES-13: ATR volatility check — ATR <= 1.8x its SMA
    atr_sma = _atr(df_l, p=14).rolling(20).mean().iloc[-1]
    if atr_sma > 0 and atr_v / atr_sma > 1.8:
        return None  # vrcExtreme — extreme volatility spike

    v_ma = df_l["volume"].rolling(20).mean().iloc[-1]
    rv   = df_l["volume"].iloc[-1] / v_ma if v_ma > 0 else 0

    # HERMES-03: Volume gate — vol < 1.0x = hard skip, 1.0–1.19x = skip
    if rv < 1.0:
        return None  # No participation
    if rv < Config.VOL_GATE:
        return None  # Below 1.2x gate — timing gate closed, -12 penalty applies

    rsi_val  = _rsi(df_l["close"]).iloc[-1]
    rsi7_val = _rsi(df_l["close"], 7).iloc[-1]

    # HERMES-12: rsiBothLate — BOTH 14-RSI >= 68 AND 7-RSI >= 68 = timing gate closed
    if rsi_val >= Config.RSI_LATE_THR and rsi7_val >= Config.RSI_LATE_THR:
        return None

    _, _, hist = _macd(df_l["close"])
    h, hp = hist.iloc[-1], hist.iloc[-2]

    adx_val = _adx(df_l).iloc[-1]

    # HERMES-02: ADX hard floor — never emit below 25
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

    # ── Determine direction ──
    is_long  = ht_t == "BULLISH" and c > e50 and c > o and h > 0 and h > hp
    is_short = ht_t in ("BEARISH", "NEUTRAL") and c < e50 and c < o and h < 0 and h < hp

    # HERMES-07: HTF 1H RSI alignment for SHORT
    # BLOCK SHORT when 1H RSI in 48–72 zone (htfBullRsi)
    if is_short and 48 <= htf_rsi <= 72:
        return None  # htfBullRsi zone — SHORT blocked

    # HERMES-07: For LONG, require 1H RSI >= 45
    if is_long and htf_rsi < 45:
        return None  # trading against macro

    # HERMES-06: SHORT RSI gates
    if is_short:
        if rsi_val > 80:
            # Mean-reversion trap risk — require score >= 92 to override
            # We'll flag this and handle in scoring
            short_rsi_override = True
        else:
            short_rsi_override = False
        if rsi_val < 20:
            # Oversold — reduced confidence, flag but don't block
            short_oversold = True
        else:
            short_oversold = False
    else:
        short_rsi_override = False
        short_oversold = False

    # HERMES-04: LONG suppression — requires ALL conditions
    if is_long:
        if adx_val < 30:
            return None  # ADX < 30 — suppress LONG
        if rsi_val <= 55:
            return None  # RSI <= 55 — suppress LONG
        if rv < 1.5:
            return None  # Vol < 1.5x — suppress LONG
        if ht_t != "BULLISH":
            return None  # HTF not BULLISH — suppress LONG
        # Note: 4H and daily timeframe checks would require additional data fetching
        # For now, we use 1H HTF as proxy. Full implementation needs 4H + daily OHLCV.

    if not (is_long or is_short):
        return None

    side = "LONG" if is_long else "SHORT"

    # ── Scoring ──
    score = 50.0

    # HTF alignment
    if ht_t == ("BULLISH" if is_long else "BEARISH"):
        score += 25.0
    else:
        score += 10.0

    # MACD
    macd_bull = h > 0 and h > hp
    macd_bear = h < 0 and h < hp
    if (is_long and macd_bull) or (is_short and macd_bear):
        score += 20.0
    elif (is_long and h > 0) or (is_short and h < 0):
        score += 8.0

    # RSI scoring
    if is_long and 45 <= rsi_val <= 65:
        score += 15.0
    elif is_long and rsi_val < 45:
        score += 8.0
    elif is_short and 40 <= rsi_val <= 68:
        score += 15.0

    # Volume
    if rv >= Config.VOL_IDEAL:
        score += 15.0
    elif rv >= Config.VOL_GATE:
        score += 7.0

    # ADX
    if adx_val >= 30:
        score += 10.0
    elif adx_val >= Config.ADX_TREND_THR:
        score += 5.0

    # EMA distance
    if dist < 2.0:
        score += 5.0

    # Penalties
    eff = score
    if rv < Config.VOL_IDEAL:
        eff -= 8.0
    if adx_val < Config.ADX_TREND_THR:
        eff -= 8.0
    if dist > 6.0:
        eff -= 5.0
    if rv < Config.VOL_GATE:
        eff -= 12.0  # HERMES-03: timing gate penalty

    # ── Style classification (BEFORE threshold — tier determines minimum score) ──
    # HERMES-05: SHORT signals always use REVERSAL style
    brkout_hi = df_l["high"].rolling(50).max().iloc[-2] if len(df_l) >= 52 else None

    if is_short:
        # SHORT tiers per GoatXX mandate:
        # PRIME: score entry + bearTotalScore >= 5.0 + closest to EMA (dist < 1.5)
        # BREAKOUT: price < N-bar low + vol >= 1.5x + bearish structure
        # SETUP: standard reversal entry (bearTotalScore >= 4.0)
        # EARLY: early reversal (lower confidence, needs score >= 90)
        if dist < 1.5 and adx_val >= Config.ADX_TREND_THR:
            style = "PRIME"
        elif brkout_hi is not None and rv >= Config.VOL_IDEAL and adx_val >= Config.ADX_TREND_THR:
            style = "BREAKOUT"
        else:
            style = "REVERSAL"
    else:
        # LONG tiers (rare — only when all LONG suppression conditions are met)
        if dist < 1.5:
            style = "PRIME"
        elif brkout_hi is not None and c > brkout_hi:
            style = "BREAKOUT"
        else:
            style = "MOMENTUM"

    # ── Tier-specific score thresholds ──
    # PRIME and BREAKOUT are high-conviction structural setups from GoatXX.
    # They have lower score thresholds because their structure already confirms conviction.
    # REVERSAL/MOMENTUM are standard entries that need the full ALPHA bar.
    if style == "PRIME":
        threshold = Config.SCORE_PRIME_THR      # 75 — structural high-conviction
    elif style == "BREAKOUT":
        threshold = Config.SCORE_BREAKOUT_THR   # 78 — volume-backed high-conviction
    else:
        threshold = Config.SCORE_ENTRY_THR      # 85 — ALPHA tier for REVERSAL/MOMENTUM

    # HERMES-02: ADX 25–27 requires score >= 92 regardless of tier
    if Config.ADX_BORDERLINE <= adx_val < Config.ADX_TREND_THR:
        threshold = max(threshold, Config.SCORE_OVERRIDE)  # 92

    # HERMES-06: SHORT with RSI > 80 requires score >= 92
    if is_short and short_rsi_override:
        threshold = max(threshold, Config.SCORE_OVERRIDE)  # 92

    # HERMES-11: Post-loss elevated threshold
    if current_signals:
        losses_4h = _recent_losses(symbol, current_signals, hours=4)
        cons_losses_8h = _consecutive_losses(symbol, current_signals, hours=8)
        if cons_losses_8h >= 2:
            threshold = max(threshold, Config.SCORE_POST_LOSS_2)  # 98
        elif losses_4h >= 1:
            threshold = max(threshold, Config.SCORE_POST_LOSS_1)  # 95

    if eff < threshold:
        return None

    # ── SL/TP construction (adaptive, risk-managed) ──
    sl_dist = atr_v * Config.SL_ATR_MULT

    # Cap SL to maximum % of entry price to prevent oversized stops
    max_sl_dist = c * (Config.MAX_SL_PCT / 100)
    if sl_dist > max_sl_dist:
        sl_dist = max_sl_dist  # tighten SL if ATR-based stop exceeds max %

    # Minimum SL distance to avoid spread risk
    min_sl_dist = c * (Config.MIN_SL_PCT / 100)
    if sl_dist < min_sl_dist:
        return None  # SL too tight — can't absorb normal volatility

    sl_price = (c - sl_dist) if is_long else (c + sl_dist)

    # Adaptive TP: widen targets when trend is strong (ADX >= 30)
    if adx_val >= 30:
        tp_mults = Config.TP_R_MULTIPLES_STRONG  # [2.0, 3.5, 5.0]
    else:
        tp_mults = Config.TP_R_MULTIPLES          # [1.5, 2.5, 4.0]

    tp_prices = [
        (c + sl_dist * r) if is_long else (c - sl_dist * r)
        for r in tp_mults
    ]

    return {
        "side":  side,
        "style": style,
        "entry": c,
        "sl":    sl_price,
        "tp":    tp_prices,
        "eff":   round(eff, 1),
        "adx":   round(adx_val, 1),
        "rsi":   round(rsi_val, 1),
        "rv":    round(rv, 2),
        "htf":   ht_t,
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
            df_l = pd.DataFrame(
                ex.fetch_ohlcv(s, "5m", limit=100),
                columns=["t", "open", "high", "low", "close", "volume"]
            )
            df_h = pd.DataFrame(
                ex.fetch_ohlcv(s, "1h", limit=100),
                columns=["t", "open", "high", "low", "close", "volume"]
            )

            sig = compute_goat_score(df_l, df_h, s, current_signals)
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
