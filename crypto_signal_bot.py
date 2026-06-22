#!/usr/bin/env python3
"""
crypto_signal_bot.py — Hermes Scalp Engine v10.0 (NFI-Enhanced)
Strategy: Ports core principles from NostalgiaForInfinity (NFI) —
the most profitable open-source crypto strategy (98.94% WR on KuCoin).

Key NFI principles integrated:
  1. RSI-3 (not RSI-14) — catches reversals early
  2. Multi-timeframe confluence — 5m + 15m + 1h + 4h + 1d
  3. Tag-based exits — different TP/SL per signal type
  4. Grinding/averaging — rebuy at -8%, -10%, -12% to convert losses to wins
  5. More entry signals — BB+RSI extremes, StochRSI, CMF, Aroon, KST
  6. Higher score threshold — 70+ (NFI's proven sweet spot)
  7. Chaikin Money Flow — volume-weighted momentum
  8. Aroon oscillator — trend direction + strength
  9. Stochastic RSI — momentum of momentum
  10. KST (Know Sure Thing) — composite multi-ROC momentum

Timeframe: 5m primary / 15m + 1h + 4h + 1d confluence
Hold time: 15min - 2hrs (scalps) to 1-3 days (swings)
"""
import os
import json
import time
import traceback
from datetime import datetime, timezone
from collections import defaultdict
import ccxt
import pandas as pd
import numpy as np
import requests

# Windows compatibility
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

    # ── Exchange & scanning ──
    MIN_24H_VOLUME_USD  = 3_000_000
    MAX_COINS_TO_SCAN   = 80
    QUOTE_CURRENCY      = "USDT"
    # NFI uses 5m as primary — faster entries
    LTF_TIMEFRAME       = "5m"
    # Multi-timeframe for confluence
    TF_15M              = "15m"
    TF_1H               = "1h"
    TF_4H               = "4h"
    TF_1D               = "1d"
    OHLCV_LIMIT         = 200
    EXCHANGE            = "KuCoin"
    FETCH_RETRY         = 3
    CAPITAL_PER_SIGNAL  = 1000.0

    # ── Indicator params ──
    # NFI uses RSI-3 as primary (fast reversal detection)
    RSI_PERIOD_FAST     = 3
    RSI_PERIOD          = 14
    RSI_OVERSOLD        = 30
    RSI_OVERBOUGHT      = 70
    # NFI: RSI-3 oversold/overbought thresholds
    RSI3_OVERSOLD       = 10
    RSI3_OVERBOUGHT     = 90

    ADX_PERIOD          = 14
    ADX_TREND_THR       = 20
    ADX_HARD_FLOOR      = 15
    MACD_FAST           = 12
    MACD_SLOW           = 26
    MACD_SIGNAL         = 9
    STOCH_FAST_K        = 5
    STOCH_FAST_D        = 3
    CCI_PERIOD          = 20
    CCI_OVERSOLD        = -100
    CCI_OVERBOUGHT      = 100
    MFI_PERIOD          = 14
    BB_PERIOD           = 20
    BB_STD              = 2.0
    EMA_FAST            = 9
    EMA_MID             = 21
    EMA_SLOW            = 50
    ATR_PERIOD          = 14
    ATR_FLOOR_BTC       = 0.08
    ATR_FLOOR_ALT       = 0.10

    # ── Scoring thresholds ──
    # NFI's implicit threshold is ~70 (score 70-75 = 87.5% WR in our data)
    SCORE_ENTRY_THR     = 70.0   # raised from 65 — NFI's proven sweet spot
    SCORE_STRONG_THR    = 80.0   # strong signals get wider TP

    # ── Risk management ──
    SL_ATR_MULT         = 1.5
    MAX_SL_PCT          = 2.0
    MIN_SL_PCT          = 0.3
    TP_R_MULTIPLES      = [1.5, 2.0, 3.0]

    # ── NFI Grinding (position averaging) ──
    GRIND_ENABLED       = True
    GRIND_REBUY_THRESH  = [-0.08, -0.10, -0.12]  # -8%, -10%, -12%
    GRIND_REBUY_STAKE   = [0.5, 0.25, 0.125]      # decreasing stake
    GRIND_MAX_REBUYS    = 3

    # ── BTC Trend Filter ──
    BTC_TREND_TIMEFRAME = "1h"
    BTC_EMA_PERIOD      = 50
    LONG_BLOCK_BEARISH  = True

    # ── Position management ──
    MAX_OPEN_PER_PAIR   = 2
    MAX_OPEN_TOTAL      = 15
    BASE_COOLDOWN       = 300
    MIN_ENTRY_PRICE     = 0.000001

    # ── File paths ──
    STATE_FILE          = "bot_state.json"
    SIGNAL_LOG_FILE     = "signals_log.json"

    # ── Blocklist ──
    BLOCKLIST           = {"H/USDT"}


# ─────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────
def utc_now():
    return datetime.now(timezone.utc)

def _rsi(series, period=14):
    delta = series.diff()
    gain  = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs    = gain / loss.replace(0, np.nan)
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

def _stoch_fast(df, k=5, d=3):
    low_min  = df["low"].rolling(k).min()
    high_max = df["high"].rolling(k).max()
    fastk    = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    fastd    = fastk.rolling(d).mean()
    return fastk, fastd

def _cci(df, period=20):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp - tp.rolling(period).mean()) / (0.015 * tp.rolling(period).std())

def _mfi(df, period=14):
    tp    = (df["high"] + df["low"] + df["close"]) / 3
    mf    = tp * df["volume"]
    pmf   = mf.where(tp > tp.shift(1), 0).rolling(period).sum()
    nmf   = mf.where(tp < tp.shift(1), 0).rolling(period).sum()
    mfr   = pmf / nmf.replace(0, np.nan)
    return 100 - (100 / (1 + mfr))

def _bollinger_bands(df, period=20, stds=2.0):
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    return mid - std * stds, mid, mid + std * stds

def _cmo(series, period=14):
    delta = series.diff()
    gain  = delta.where(delta > 0, 0).rolling(period).sum()
    loss  = (-delta.where(delta < 0, 0)).rolling(period).sum()
    return 100 * (gain - loss) / (gain + loss).replace(0, np.nan)

# ── NFI New Indicators ──

def _stochrsi(series, rsi_period=14, stoch_period=14, k=3, d=3):
    """Stochastic RSI — NFI's key momentum indicator"""
    rsi = _rsi(series, rsi_period)
    rsi_min = rsi.rolling(stoch_period).min()
    rsi_max = rsi.rolling(stoch_period).max()
    stoch = (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan) * 100
    k_line = stoch.rolling(k).mean()
    d_line = k_line.rolling(d).mean()
    return k_line, d_line

def _aroon(df, period=14):
    """Aroon oscillator — NFI's trend direction indicator"""
    high = df["high"]
    low = df["low"]
    aroon_up = high.rolling(period).apply(lambda x: (period - np.argmax(x)) / period * 100, raw=True)
    aroon_down = low.rolling(period).apply(lambda x: (period - np.argmin(x)) / period * 100, raw=True)
    return aroon_up, aroon_down

def _cmf(df, period=20):
    """Chaikin Money Flow — NFI's volume-weighted momentum"""
    mf_mult = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"]).replace(0, np.nan)
    mf_vol = mf_mult * df["volume"]
    return mf_vol.rolling(period).sum() / df["volume"].rolling(period).sum()

def _kst(df, period=10):
    """KST (Know Sure Thing) — NFI's composite momentum"""
    roc1 = df["close"].pct_change(10) * 100
    roc2 = df["close"].pct_change(15) * 100
    roc3 = df["close"].pct_change(20) * 100
    roc4 = df["close"].pct_change(30) * 100
    sma1 = roc1.rolling(period).mean()
    sma2 = roc2.rolling(period).mean()
    sma3 = roc3.rolling(period).mean()
    sma4 = roc4.rolling(15).mean()
    kst = sma1 + 2*sma2 + 3*sma3 + 4*sma4
    signal = kst.rolling(9).mean()
    return kst, signal


def _check_btc_trend(ex):
    """Check BTC macro trend across multiple timeframes (NFI-style)"""
    try:
        # Check 1h trend
        btc_1h = pd.DataFrame(ex.fetch_ohlcv("BTC/USDT", "1h", limit=100),
                              columns=["t", "open", "high", "low", "close", "volume"])
        btc_ema50 = btc_1h["close"].ewm(span=50, adjust=False).mean().iloc[-1]
        btc_ema200 = btc_1h["close"].ewm(span=200, adjust=False).mean().iloc[-1]
        btc_price = btc_1h["close"].iloc[-1]

        # Check 4h trend
        btc_4h = pd.DataFrame(ex.fetch_ohlcv("BTC/USDT", "4h", limit=100),
                              columns=["t", "open", "high", "low", "close", "volume"])
        btc_4h_ema50 = btc_4h["close"].ewm(span=50, adjust=False).mean().iloc[-1]
        btc_4h_price = btc_4h["close"].iloc[-1]

        # Multi-TF confluence (NFI-style)
        bull_1h = btc_price > btc_ema50
        bull_4h = btc_4h_price > btc_4h_ema50
        ema_stack = btc_ema50 > btc_ema200

        is_bullish = bull_1h and bull_4h and ema_stack
        is_bearish = not bull_1h and not bull_4h and not ema_stack

        return is_bullish, is_bearish
    except Exception as e:
        print(f"[BTC-TREND] Failed: {e}")
        return True, True  # fail-open: allow both directions


# ─────────────────────────────────────────────
#  Signal logger
# ─────────────────────────────────────────────
def _load_signals():
    if not os.path.exists(Config.SIGNAL_LOG_FILE):
        return []
    try:
        with open(Config.SIGNAL_LOG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def log_signal(symbol, sig):
    log = _load_signals()
    entry = {
        "id":         f"SIG-{len(log)+1:04d}",
        "time":       utc_now().isoformat(),
        "pair":       symbol,
        "exchange":   Config.EXCHANGE,
        "direction":  sig["side"],
        "style":      sig["style"],
        "score":      sig["eff"],
        "adx":        sig["adx"],
        "rsi":        sig["rsi"],
        "rsi3":       sig.get("rsi3", 0),
        "volume_x":   sig["rv"],
        "htf_trend":  sig["htf"],
        "entry":      sig["entry"],
        "sl":         sig["sl"],
        "tp1":        sig["tp"][0] if len(sig["tp"]) > 0 else None,
        "tp2":        sig["tp"][1] if len(sig["tp"]) > 1 else None,
        "tp3":        sig["tp"][2] if len(sig["tp"]) > 2 else None,
        "tp_main":    sig["tp"][-1] if sig["tp"] else None,
        "capital":    Config.CAPITAL_PER_SIGNAL,
        "status":     "OPEN",
        "exit_price": None,
        "pnl_usd":    None,
        "result":     None,
        "closed_at":  None,
        "exit_time":  None,
        # NFI grinding fields
        "grind_count": 0,
        "grind_entries": [sig["entry"]],
        "tag": sig.get("tag", ""),
    }
    log.append(entry)
    with open(Config.SIGNAL_LOG_FILE, "w") as f:
        _file_lock(f)
        json.dump(log, f, indent=2)
        _file_unlock(f)
    print(f"[LOG] {entry['id']} {symbol} {sig['style']} score={sig['eff']}")
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
                with open(self.path, "r") as f:
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
#  NFI-Enhanced Multi-Strategy Engine
# ─────────────────────────────────────────────
def _compute_indicators_multi_tf(ex, symbol):
    """
    NFI-style multi-timeframe indicator computation.
    Fetches 5m, 15m, 1h, 4h, 1d data for full confluence.
    Returns dict of all indicators, or None on failure.
    """
    try:
        df_5m  = pd.DataFrame(ex.fetch_ohlcv(symbol, "5m",  limit=200),
                              columns=["t","open","high","low","close","volume"])
        df_15m = pd.DataFrame(ex.fetch_ohlcv(symbol, "15m", limit=200),
                              columns=["t","open","high","low","close","volume"])
        df_1h  = pd.DataFrame(ex.fetch_ohlcv(symbol, "1h",  limit=200),
                              columns=["t","open","high","low","close","volume"])
        df_4h  = pd.DataFrame(ex.fetch_ohlcv(symbol, "4h",  limit=200),
                              columns=["t","open","high","low","close","volume"])
        df_1d  = pd.DataFrame(ex.fetch_ohlcv(symbol, "1d",  limit=100),
                              columns=["t","open","high","low","close","volume"])
    except Exception as e:
        print(f"  [FETCH] {symbol}: {e}")
        return None

    try:
        c = df_5m["close"].iloc[-1]

        # ── 5m (primary) indicators ──
        rsi_14  = _rsi(df_5m["close"], 14).iloc[-1]
        rsi_3   = _rsi(df_5m["close"], 3).iloc[-1]   # NFI's key
        rsi_7   = _rsi(df_5m["close"], 7).iloc[-1]
        adx     = _adx(df_5m, 14).iloc[-1]
        atr     = _atr(df_5m, 14).iloc[-1]
        atr_pct = (atr / c) * 100 if c > 0 else 0

        macd_line, macd_sig, macd_hist = _macd(df_5m["close"])
        mh  = macd_hist.iloc[-1]
        mhp = macd_hist.iloc[-2]

        stoch_k, stoch_d = _stoch_fast(df_5m, 5, 3)
        sk = stoch_k.iloc[-1]; sd = stoch_d.iloc[-1]

        cci_v  = _cci(df_5m, 20).iloc[-1]
        mfi_v  = _mfi(df_5m, 14).iloc[-1]
        cmo_v  = _cmo(df_5m["close"], 14).iloc[-1]

        bb_lower, bb_mid, bb_upper = _bollinger_bands(df_5m, 20, 2.0)
        bbl = bb_lower.iloc[-1]; bbm = bb_mid.iloc[-1]; bbu = bb_upper.iloc[-1]
        bb_width = (bbu - bbl) / bbm * 100 if bbm > 0 else 0

        ema_9  = df_5m["close"].ewm(span=9,  adjust=False).mean().iloc[-1]
        ema_21 = df_5m["close"].ewm(span=21, adjust=False).mean().iloc[-1]
        ema_50 = df_5m["close"].ewm(span=50, adjust=False).mean().iloc[-1]

        v_ma = df_5m["volume"].rolling(20).mean().iloc[-1]
        rv   = df_5m["volume"].iloc[-1] / v_ma if v_ma > 0 else 0

        # NFI new indicators on 5m
        stochrsi_k, stochrsi_d = _stochrsi(df_5m["close"], 14, 14, 3, 3)
        srsi_k = stochrsi_k.iloc[-1]; srsi_d = stochrsi_d.iloc[-1]

        aroon_up, aroon_down = _aroon(df_5m, 14)
        aroon_osc = aroon_up.iloc[-1] - aroon_down.iloc[-1]

        cmf_v = _cmf(df_5m, 20).iloc[-1]

        kst_v, kst_sig = _kst(df_5m)
        kst_val = kst_v.iloc[-1]; kst_signal = kst_sig.iloc[-1]

        # ── 15m indicators ──
        rsi_15m = _rsi(df_15m["close"], 3).iloc[-1]
        ema_15m_50 = df_15m["close"].ewm(span=50, adjust=False).mean().iloc[-1]
        c_15m = df_15m["close"].iloc[-1]

        # ── 1h indicators ──
        ht_c    = df_1h["close"].iloc[-1]
        ht_e50  = df_1h["close"].ewm(span=50,  adjust=False).mean().iloc[-1]
        ht_e200 = df_1h["close"].ewm(span=200, adjust=False).mean().iloc[-1]
        ht_rsi  = _rsi(df_1h["close"], 14).iloc[-1]
        ht_rsi3 = _rsi(df_1h["close"], 3).iloc[-1]
        ht_adx  = _adx(df_1h, 14).iloc[-1]

        # ── 4h indicators ──
        fh_c    = df_4h["close"].iloc[-1]
        fh_e50  = df_4h["close"].ewm(span=50, adjust=False).mean().iloc[-1]
        fh_rsi3 = _rsi(df_4h["close"], 3).iloc[-1]

        # ── 1d indicators ──
        dc      = df_1d["close"].iloc[-1]
        de_200  = df_1d["close"].ewm(span=200, adjust=False).mean().iloc[-1]
        drsi3   = _rsi(df_1d["close"], 3).iloc[-1]

        # HTF trend (NFI-style multi-TF)
        ht_t = (
            "BULLISH"  if ht_c > ht_e50 > ht_e200 else
            "BEARISH"  if ht_c < ht_e50 < ht_e200 else
            "NEUTRAL"
        )

        # Golden / death cross detection
        ht_e50_prev  = df_1h["close"].ewm(span=50,  adjust=False).mean().iloc[-5]
        ht_e200_prev = df_1h["close"].ewm(span=200, adjust=False).mean().iloc[-5]
        golden_cross = ht_e50_prev < ht_e200_prev and ht_e50 > ht_e200
        death_cross  = ht_e50_prev > ht_e200_prev and ht_e50 < ht_e200

        # Range for breakout
        hi_20 = df_5m["high"].rolling(20).max().iloc[-2]
        lo_20 = df_5m["low"].rolling(20).min().iloc[-2]

        return {
            "c": c,
            "rsi3": rsi_3, "rsi7": rsi_7, "rsi14": rsi_14,
            "adx": adx, "atr": atr, "atr_pct": atr_pct,
            "mh": mh, "mhp": mhp,
            "sk": sk, "sd": sd,
            "cci": cci_v, "mfi": mfi_v, "cmo": cmo_v,
            "bbl": bbl, "bbm": bbm, "bbu": bbu, "bb_width": bb_width,
            "ema9": ema_9, "ema21": ema_21, "ema50": ema_50,
            "rv": rv,
            # NFI new
            "srsi_k": srsi_k, "srsi_d": srsi_d,
            "aroon_osc": aroon_osc,
            "cmf": cmf_v,
            "kst": kst_val, "kst_sig": kst_signal,
            # Multi-TF
            "rsi3_15m": rsi_15m, "c_15m": c_15m, "ema50_15m": ema_15m_50,
            "ht_c": ht_c, "ht_e50": ht_e50, "ht_e200": ht_e200,
            "ht_rsi": ht_rsi, "ht_rsi3": ht_rsi3, "ht_adx": ht_adx, "ht_t": ht_t,
            "fh_c": fh_c, "fh_e50": fh_e50, "fh_rsi3": fh_rsi3,
            "dc": dc, "de200": de_200, "drsi3": drsi3,
            "golden_cross": golden_cross, "death_cross": death_cross,
            "hi_20": hi_20, "lo_20": lo_20,
        }
    except Exception as e:
        print(f"  [IND] {symbol}: {e}")
        return None


def _build_sl_tp(c, atr, direction, style, score):
    """Build SL/TP with NFI-style tag-based exits"""
    sl_dist = atr * Config.SL_ATR_MULT

    # NFI: different SL/TP per signal type
    if style in ("NFI_RSI3_EXTREME", "NFI_STOCHRSI"):
        # Tight exits for reversal signals
        tp_mults = [1.0, 1.5, 2.0]
    elif style in ("NFI_BB_REVERSION", "NFI_CMF"):
        # Medium exits
        tp_mults = [1.5, 2.0, 3.0]
    elif style in ("NFI_BREAKOUT", "NFI_KST"):
        # Wide exits for momentum
        tp_mults = [2.0, 3.0, 5.0]
    else:
        tp_mults = Config.TP_R_MULTIPLES

    # Strong signals get wider TP
    if score >= Config.SCORE_STRONG_THR:
        tp_mults = [m * 1.5 for m in tp_mults]

    max_sl = c * (Config.MAX_SL_PCT / 100)
    min_sl = c * (Config.MIN_SL_PCT / 100)
    sl_dist = min(sl_dist, max_sl)
    if sl_dist < min_sl:
        return None

    sl_price = (c - sl_dist) if direction == "LONG" else (c + sl_dist)
    tp_prices = [
        (c + sl_dist * r) if direction == "LONG" else (c - sl_dist * r)
        for r in tp_mults
    ]

    return {"sl": sl_price, "tp": tp_prices}


def _score_signal_nfi(ind, direction, style, regime):
    """
    NFI-style scoring: start at 100, subtract for bad conditions.
    Key insight from our data: score 70-75 = 87.5% WR (NFI's sweet spot)
    """
    score = 100.0
    is_long = direction == "LONG"

    # ── ADX: penalize low trend strength ──
    if ind["adx"] < 15:
        score -= 20
    elif ind["adx"] < 20:
        score -= 10

    # ── Volume: penalize low volume ──
    if ind["rv"] < 0.5:
        score -= 20
    elif ind["rv"] < 1.0:
        score -= 10

    # ── RSI-3 extreme check (NFI's core) ──
    if is_long:
        if ind["rsi3"] > 20:  # Not oversold enough
            score -= 15
        if ind["rsi3"] < 5:   # Too extreme, might be crashing
            score -= 5
    else:
        if ind["rsi3"] < 80:  # Not overbought enough
            score -= 15
        if ind["rsi3"] > 95:  # Too extreme
            score -= 5

    # ── StochRSI confirmation (NFI) ──
    if is_long:
        if ind["srsi_k"] > 20:
            score -= 10
    else:
        if ind["srsi_k"] < 80:
            score -= 10

    # ── Aroon trend (NFI) ──
    if is_long:
        if ind["aroon_osc"] < 0:
            score -= 10
    else:
        if ind["aroon_osc"] > 0:
            score -= 10

    # ── CMF money flow (NFI) ──
    if is_long:
        if ind["cmf"] < 0:
            score -= 10
    else:
        if ind["cmf"] > 0:
            score -= 10

    # ── KST momentum (NFI) ──
    if is_long:
        if ind["kst"] < ind["kst_sig"]:
            score -= 8
    else:
        if ind["kst"] > ind["kst_sig"]:
            score -= 8

    # ── Multi-TF confluence (NFI's key) ──
    # 15m RSI-3 should agree
    if is_long and ind["rsi3_15m"] > 30:
        score -= 8
    if not is_long and ind["rsi3_15m"] < 70:
        score -= 8

    # ── HARD TREND BLOCKS (NFI principle: never fight the trend) ──
    # If 4H and daily both strongly against trade, REJECT entirely
    trend_against = 0
    if is_long:
        if ind["fh_c"] < ind["fh_e50"]:  # 4H bearish
            trend_against += 1
        if ind["dc"] < ind["de200"]:  # Daily bearish
            trend_against += 1
        if ind["ht_t"] == "BEARISH":  # 1H bearish
            trend_against += 1
    else:  # SHORT
        if ind["fh_c"] > ind["fh_e50"]:  # 4H bullish
            trend_against += 1
        if ind["dc"] > ind["de200"]:  # Daily bullish
            trend_against += 1
        if ind["ht_t"] == "BULLISH":  # 1H bullish
            trend_against += 1

    # If 2+ timeframes are against the trade, heavy penalty
    if trend_against >= 2:
        score -= 30  # Essentially blocks the signal
    elif trend_against == 1:
        score -= 15

    # ── HTF alignment (regime-aware) ──
    regime_bearish = regime in ("EXTREME_FEAR", "BEARISH")
    regime_bullish = regime == "BULLISH"

    if is_long:
        if ind["ht_t"] == "BEARISH" and not regime_bullish:
            score -= 10
    else:  # SHORT
        if ind["ht_t"] == "BULLISH" and not regime_bearish:
            score -= 10

    # ── MACD confirmation ──
    if is_long and ind["mh"] < 0:
        score -= 10
    elif not is_long and ind["mh"] > 0:
        score -= 10

    # ── Distance from EMA50 ──
    dist_from_ema = abs(ind["c"] - ind["ema50"]) / ind["ema50"] * 100
    if dist_from_ema > 10:
        score -= 15
    elif dist_from_ema > 6:
        score -= 5

    return round(score, 1)


def compute_scalp_signals_nfi(df_l, df_h, symbol, regime, ex):
    """
    NFI-enhanced scalping engine.
    Uses multi-timeframe confluence + more entry signals + tag-based exits.
    """
    # We need 5m data — df_l should be 5m now
    if len(df_l) < 50:
        return []

    try:
        # Fetch all timeframes
        df_15m = pd.DataFrame(ex.fetch_ohlcv(symbol, "15m", limit=200),
                              columns=["t","open","high","low","close","volume"])
        df_1h  = pd.DataFrame(ex.fetch_ohlcv(symbol, "1h",  limit=200),
                              columns=["t","open","high","low","close","volume"])
        df_4h  = pd.DataFrame(ex.fetch_ohlcv(symbol, "4h",  limit=200),
                              columns=["t","open","high","low","close","volume"])
        df_1d  = pd.DataFrame(ex.fetch_ohlcv(symbol, "1d",  limit=100),
                              columns=["t","open","high","low","close","volume"])
    except Exception:
        return []

    try:
        ind = _compute_indicators_multi_tf(ex, symbol)
    except Exception:
        return []

    if ind is None:
        return []

    c = ind["c"]

    # Pre-filters
    if c < Config.MIN_ENTRY_PRICE:
        return []
    if ind["atr_pct"] < (Config.ATR_FLOOR_BTC if "BTC" in symbol else Config.ATR_FLOOR_ALT):
        return []
    if ind["adx"] < Config.ADX_HARD_FLOOR:
        return []

    is_bearish = regime in ("EXTREME_FEAR", "BEARISH")
    is_bullish = regime == "BULLISH"

    candidates = []

    # ════════════════════════════════════════════
    #  NFI SIGNAL 1: RSI-3 Extreme Reversal
    #  RSI-3 < 10 (LONG) or > 90 (SHORT) + StochRSI confirmation
    # ════════════════════════════════════════════
    if ind["rsi3"] < Config.RSI3_OVERSOLD and ind["srsi_k"] < 20:
        if not is_bearish:
            score = _score_signal_nfi(ind, "LONG", "NFI_RSI3_EXTREME", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "LONG", "NFI_RSI3_EXTREME", score)
                if sltp:
                    candidates.append({"side":"LONG","style":"NFI_RSI3_EXTREME","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"nfi_rsi3_long"})

    if ind["rsi3"] > Config.RSI3_OVERBOUGHT and ind["srsi_k"] > 80:
        if not is_bullish:
            score = _score_signal_nfi(ind, "SHORT", "NFI_RSI3_EXTREME", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "SHORT", "NFI_RSI3_EXTREME", score)
                if sltp:
                    candidates.append({"side":"SHORT","style":"NFI_RSI3_EXTREME","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"nfi_rsi3_short"})

    # ════════════════════════════════════════════
    #  NFI SIGNAL 2: BB + RSI-3 Mean Reversion
    #  Price at BB lower band + RSI-3 oversold (LONG)
    #  Price at BB upper band + RSI-3 overbought (SHORT)
    # ════════════════════════════════════════════
    if c <= ind["bbl"] * 1.01 and ind["rsi3"] < 20 and ind["cmf"] > -0.1:
        if not is_bearish:
            score = _score_signal_nfi(ind, "LONG", "NFI_BB_REVERSION", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "LONG", "NFI_BB_REVERSION", score)
                if sltp:
                    candidates.append({"side":"LONG","style":"NFI_BB_REVERSION","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"nfi_bb_long"})

    if c >= ind["bbu"] * 0.99 and ind["rsi3"] > 80 and ind["cmf"] < 0.1:
        if not is_bullish:
            score = _score_signal_nfi(ind, "SHORT", "NFI_BB_REVERSION", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "SHORT", "NFI_BB_REVERSION", score)
                if sltp:
                    candidates.append({"side":"SHORT","style":"NFI_BB_REVERSION","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"nfi_bb_short"})

    # ════════════════════════════════════════════
    #  NFI SIGNAL 3: StochRSI Crossover
    #  StochRSI K crosses above D in oversold (LONG)
    #  StochRSI K crosses below D in overbought (SHORT)
    # ════════════════════════════════════════════
    if ind["srsi_k"] > ind["srsi_d"] and ind["srsi_k"] < 25 and ind["rsi3"] < 30:
        if not is_bearish:
            score = _score_signal_nfi(ind, "LONG", "NFI_STOCHRSI", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "LONG", "NFI_STOCHRSI", score)
                if sltp:
                    candidates.append({"side":"LONG","style":"NFI_STOCHRSI","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"nfi_srsi_long"})

    if ind["srsi_k"] < ind["srsi_d"] and ind["srsi_k"] > 75 and ind["rsi3"] > 70:
        if not is_bullish:
            score = _score_signal_nfi(ind, "SHORT", "NFI_STOCHRSI", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "SHORT", "NFI_STOCHRSI", score)
                if sltp:
                    candidates.append({"side":"SHORT","style":"NFI_STOCHRSI","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"nfi_srsi_short"})

    # ════════════════════════════════════════════
    #  NFI SIGNAL 4: CMF + Aroon Trend
    #  CMF turning positive + Aroon uptrend (LONG)
    #  CMF turning negative + Aroon downtrend (SHORT)
    # ════════════════════════════════════════════
    if ind["cmf"] > 0 and ind["aroon_osc"] > 20 and ind["mh"] > 0:
        if not is_bearish:
            score = _score_signal_nfi(ind, "LONG", "NFI_CMF", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "LONG", "NFI_CMF", score)
                if sltp:
                    candidates.append({"side":"LONG","style":"NFI_CMF","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"nfi_cmf_long"})

    if ind["cmf"] < 0 and ind["aroon_osc"] < -20 and ind["mh"] < 0:
        if not is_bullish:
            score = _score_signal_nfi(ind, "SHORT", "NFI_CMF", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "SHORT", "NFI_CMF", score)
                if sltp:
                    candidates.append({"side":"SHORT","style":"NFI_CMF","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"nfi_cmf_short"})

    # ════════════════════════════════════════════
    #  NFI SIGNAL 5: KST Momentum Breakout
    #  KST crosses above signal + ADX > 20 (LONG)
    #  KST crosses below signal + ADX > 20 (SHORT)
    # ════════════════════════════════════════════
    if ind["kst"] > ind["kst_sig"] and ind["adx"] >= 20 and ind["rv"] >= 1.2:
        if not is_bearish:
            score = _score_signal_nfi(ind, "LONG", "NFI_KST", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "LONG", "NFI_KST", score)
                if sltp:
                    candidates.append({"side":"LONG","style":"NFI_KST","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"nfi_kst_long"})

    if ind["kst"] < ind["kst_sig"] and ind["adx"] >= 20 and ind["rv"] >= 1.2:
        if not is_bullish:
            score = _score_signal_nfi(ind, "SHORT", "NFI_KST", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "SHORT", "NFI_KST", score)
                if sltp:
                    candidates.append({"side":"SHORT","style":"NFI_KST","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"nfi_kst_short"})

    # ════════════════════════════════════════════
    #  ORIGINAL SIGNALS (kept for compatibility)
    #  EMA_PULLBACK, BB_SQUEEZE, MOMENTUM_BREAK
    # ════════════════════════════════════════════

    # EMA_PULLBACK SHORT
    short_ema = (c < ind["ema50"] and c >= ind["ema9"] * 0.98 and ind["mh"] < 0)
    if short_ema and not is_bullish:
        score = _score_signal_nfi(ind, "SHORT", "EMA_PULLBACK", regime)
        if score >= Config.SCORE_ENTRY_THR:
            sltp = _build_sl_tp(c, ind["atr"], "SHORT", "EMA_PULLBACK", score)
            if sltp:
                candidates.append({"side":"SHORT","style":"EMA_PULLBACK","entry":c,
                    "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                    "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                    "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                    "htf":ind["ht_t"],"tag":"ema_short"})

    # EMA_PULLBACK LONG
    long_ema = (c > ind["ema50"] and c <= ind["ema9"] * 1.02 and ind["mh"] > 0)
    if long_ema and not is_bearish:
        score = _score_signal_nfi(ind, "LONG", "EMA_PULLBACK", regime)
        if score >= Config.SCORE_ENTRY_THR:
            sltp = _build_sl_tp(c, ind["atr"], "LONG", "EMA_PULLBACK", score)
            if sltp:
                candidates.append({"side":"LONG","style":"EMA_PULLBACK","entry":c,
                    "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                    "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                    "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                    "htf":ind["ht_t"],"tag":"ema_long"})

    # BB_SQUEEZE SHORT
    if ind["bb_width"] < 4.0 and c < ind["bbm"] and ind["mh"] < 0 and not is_bullish:
        score = _score_signal_nfi(ind, "SHORT", "BB_SQUEEZE", regime)
        if score >= Config.SCORE_ENTRY_THR:
            sltp = _build_sl_tp(c, ind["atr"], "SHORT", "BB_SQUEEZE", score)
            if sltp:
                candidates.append({"side":"SHORT","style":"BB_SQUEEZE","entry":c,
                    "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                    "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                    "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                    "htf":ind["ht_t"],"tag":"bb_short"})

    # BB_SQUEEZE LONG
    if ind["bb_width"] < 4.0 and c > ind["bbm"] and ind["mh"] > 0 and not is_bearish:
        score = _score_signal_nfi(ind, "LONG", "BB_SQUEEZE", regime)
        if score >= Config.SCORE_ENTRY_THR:
            sltp = _build_sl_tp(c, ind["atr"], "LONG", "BB_SQUEEZE", score)
            if sltp:
                candidates.append({"side":"LONG","style":"BB_SQUEEZE","entry":c,
                    "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                    "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                    "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                    "htf":ind["ht_t"],"tag":"bb_long"})

    # MOMENTUM_BREAK
    if ind["adx"] >= 20:
        if c < ind["lo_20"] and not is_bullish:
            score = _score_signal_nfi(ind, "SHORT", "MOMENTUM_BREAK", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "SHORT", "MOMENTUM_BREAK", score)
                if sltp:
                    candidates.append({"side":"SHORT","style":"MOMENTUM_BREAK","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"mom_short"})
        if c > ind["hi_20"] and not is_bearish:
            score = _score_signal_nfi(ind, "LONG", "MOMENTUM_BREAK", regime)
            if score >= Config.SCORE_ENTRY_THR:
                sltp = _build_sl_tp(c, ind["atr"], "LONG", "MOMENTUM_BREAK", score)
                if sltp:
                    candidates.append({"side":"LONG","style":"MOMENTUM_BREAK","entry":c,
                        "sl":sltp["sl"],"tp":sltp["tp"],"eff":score,
                        "adx":round(ind["adx"],1),"rsi":round(ind["rsi14"],1),
                        "rsi3":round(ind["rsi3"],1),"rv":round(ind["rv"],2),
                        "htf":ind["ht_t"],"tag":"mom_long"})

    if not candidates:
        return []

    # Sort by score, take top 2
    candidates.sort(key=lambda s: -s["eff"])
    return candidates[:2]


# ─────────────────────────────────────────────
#  NFI Grinding Engine
# ─────────────────────────────────────────────
def process_grinding(signals, ex, notifier):
    """
    NFI-style grinding: check open signals for rebuy opportunities.
    When a losing trade drops -8%, -10%, -12%, add to position.
    """
    if not Config.GRIND_ENABLED:
        return

    for sig in signals:
        if sig.get("status") != "OPEN":
            continue
        if sig.get("grind_count", 0) >= Config.GRIND_MAX_REBUYS:
            continue

        try:
            pair = sig["pair"]
            direction = sig["direction"]
            current_price = ex.fetch_ticker(pair)["last"]

            # Calculate PnL
            entry = sig["entry"]
            if direction == "LONG":
                pnl_pct = (current_price - entry) / entry
            else:
                pnl_pct = (entry - current_price) / entry

            # Check if we should rebuy
            grind_count = sig.get("grind_count", 0)
            if grind_count < len(Config.GRIND_REBUY_THRESH):
                threshold = Config.GRIND_REBUY_THRESH[grind_count]
                if pnl_pct <= threshold:
                    # Rebuy!
                    stake_mult = Config.GRIND_REBUY_STAKE[grind_count]
                    new_stake = Config.CAPITAL_PER_SIGNAL * stake_mult

                    # Update signal
                    sig["grind_count"] = grind_count + 1
                    sig["grind_entries"].append(current_price)

                    # Calculate new average entry
                    entries = sig["grind_entries"]
                    weights = [1.0] + [stake_mult] * grind_count
                    avg_entry = sum(e * w for e, w in zip(entries, weights)) / sum(weights)
                    sig["entry"] = avg_entry

                    # Recalculate SL from new entry
                    atr = sig.get("atr", entry * 0.02)
                    sl_dist = atr * Config.SL_ATR_MULT
                    sig["sl"] = (avg_entry - sl_dist) if direction == "LONG" else (avg_entry + sl_dist)

                    msg = (
                        f"🔄 <b>GRIND REBUY #{grind_count+1}</b>\n"
                        f"Pair: <code>{pair}</code> {direction}\n"
                        f"PnL: {pnl_pct:.2%}\n"
                        f"New Entry: <code>{avg_entry:.8f}</code>\n"
                        f"Stake: ${new_stake:.0f} ({stake_mult:.0%})\n"
                        f"SL: <code>{sig['sl']:.8f}</code>"
                    )
                    notifier.send(msg)
                    print(f"[GRIND] {pair} rebuy #{grind_count+1} at {current_price:.8f} (PnL: {pnl_pct:.2%})")

        except Exception as e:
            print(f"[GRIND] Error processing {sig.get('pair','?')}: {e}")


# ─────────────────────────────────────────────
#  Main
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

    nt.send(
        f"🤖 <b>Hermes NFI-Enhanced v10.0</b>{nl}"
        f"Exchange: {Config.EXCHANGE}{nl}"
        f"Pairs: {len(syms)} | TF: {Config.LTF_TIMEFRAME}+{Config.TF_15M}+{Config.TF_1H}+{Config.TF_4H}+{Config.TF_1D}{nl}"
        f"Score Threshold: {Config.SCORE_ENTRY_THR}{nl}"
        f"Grinding: {'ON' if Config.GRIND_ENABLED else 'OFF'}"
    )

    # ── Market Pre-Scan ──
    btc_regime = None
    try:
        import importlib.util
        prescan_path = os.path.join(Config.SCRIPT_DIR, "market_pre_scan.py")
        if os.path.exists(prescan_path):
            spec = importlib.util.spec_from_file_location("market_pre_scan", prescan_path)
            prescan_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(prescan_mod)
            btc_regime = prescan_mod.analyze_market_regime()
    except Exception as e:
        print(f"[PRESCAN] Failed: {e}")

    if btc_regime is None:
        btc_regime = {"regime": "SIDEWAYS", "long_multiplier": 0.5, "short_multiplier": 0.8}

    regime = btc_regime.get("regime", "SIDEWAYS")
    long_mult = btc_regime.get("long_multiplier", 1.0)
    short_mult = btc_regime.get("short_multiplier", 1.0)

    print(f"[REGIME] {regime} | LONG x{long_mult} | SHORT x{short_mult}")

    # ── BTC Multi-TF Trend Check ──
    btc_bullish, btc_bearish = _check_btc_trend(ex)
    print(f"[BTC-TREND] Bullish: {btc_bullish} | Bearish: {btc_bearish}")

    if len(syms) == 0:
        nt.send("⚠️ No pairs found above volume threshold!")
        return

    current_signals = _load_signals()
    total_open = sum(1 for s in current_signals if s.get("status") == "OPEN")

    # ── Process grinding for existing open signals ──
    process_grinding(current_signals, ex, nt)

    signals_sent = 0
    style_counts = defaultdict(int)
    skip_reasons = defaultdict(int)

    for s in syms:
        if s in Config.BLOCKLIST:
            skip_reasons["blocklist"] += 1
            continue
        if state.is_on_cooldown(s):
            skip_reasons["cooldown"] += 1
            continue

        pair_open = sum(1 for sig in current_signals if sig.get("pair") == s and sig.get("status") == "OPEN")
        if pair_open >= Config.MAX_OPEN_PER_PAIR:
            skip_reasons["pair_max"] += 1
            continue
        if total_open >= Config.MAX_OPEN_TOTAL:
            skip_reasons["total_max"] += 1
            continue

        try:
            # Fetch 5m data for the main signal computation
            df_5m = pd.DataFrame(
                ex.fetch_ohlcv(s, Config.LTF_TIMEFRAME, limit=Config.OHLCV_LIMIT),
                columns=["t", "open", "high", "low", "close", "volume"]
            )
            df_1h = pd.DataFrame(
                ex.fetch_ohlcv(s, Config.TF_1H, limit=Config.OHLCV_LIMIT),
                columns=["t", "open", "high", "low", "close", "volume"]
            )

            scalp_signals = compute_scalp_signals_nfi(df_5m, df_1h, s, regime, ex)

            # Apply regime filters
            if long_mult == 0.0:
                scalp_signals = [sig for sig in scalp_signals if sig["side"] != "LONG"]
            elif long_mult < 1.0:
                long_sigs = [sig for sig in scalp_signals if sig["side"] == "LONG"]
                short_sigs = [sig for sig in scalp_signals if sig["side"] == "SHORT"]
                long_sigs = [sig for sig in long_sigs if sig["eff"] >= Config.SCORE_ENTRY_THR]
                scalp_signals = long_sigs + short_sigs

            for sig in scalp_signals:
                sig_id = log_signal(s, sig)
                state.record_and_save(s)
                current_signals = _load_signals()
                total_open += 1

                m = (
                    f"⚡ <b>{sig['side']} SCALP</b>{nl}"
                    f"Pair: <code>{s}</code>{nl}"
                    f"Style: {sig['style']}{nl}"
                    f"Score: {sig['eff']}{nl}"
                    f"RSI-3: {sig.get('rsi3', 'N/A')} | RSI-14: {sig['rsi']}{nl}"
                    f"HTF: {sig['htf']} | ADX: {sig['adx']} | Vol: {sig['rv']}x{nl}"
                    f"Entry: <code>{sig['entry']:.8f}</code>{nl}"
                    f"SL: <code>{sig['sl']:.8f}</code>"
                )
                for i, p in enumerate(sig["tp"]):
                    m += f"{nl}TP{i+1}: <code>{p:.8f}</code>"

                nt.send(m)
                signals_sent += 1
                style_counts[sig["style"]] += 1

        except Exception as e:
            skip_reasons["error"] += 1
            print(f"Error scanning {s}: {e}")
            continue

    # ── Summary report ──
    if signals_sent > 0 or sum(skip_reasons.values()) > 0:
        style_str = " | ".join(f"{k}: {v}" for k, v in sorted(style_counts.items(), key=lambda x: -x[1]))
        skip_str = " | ".join(f"{k}: {v}" for k, v in sorted(skip_reasons.items(), key=lambda x: -x[1]) if v > 0)

        report = (
            f"{'✅' if signals_sent > 0 else '⚠️'} <b>Scan Complete</b>{nl}"
            f"Scanned: {len(syms)} pairs | Signals: {signals_sent}{nl}"
        )
        if style_str:
            report += f"Styles: {style_str}{nl}"
        if skip_str:
            report += f"Skipped: {skip_str}{nl}"

        nt.send(report)


if __name__ == "__main__":
    main()
