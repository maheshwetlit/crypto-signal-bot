#!/usr/bin/env python3
import os
import json
import time
import traceback
from datetime import datetime, timezone
import ccxt
import pandas as pd
import numpy as np
import requests

# --- Config ---
class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    MIN_24H_VOLUME_USD = 5_000_000
    MAX_COINS_TO_SCAN = 60
    QUOTE_CURRENCY = "USDT"
    LTF_TIMEFRAME = "5m"
    HTF_TIMEFRAME = "1h"
    OHLCV_LIMIT = 500
    # FIX 1: Relaxed ATR floors — 0.4% for alts was killing all low-vol setups in ranging markets
    ATR_FLOOR_BTC = 0.10
    ATR_FLOOR_ALT = 0.15
    # FIX 2: Relaxed EMA distance — 8% was too tight for trending breakouts
    MAX_EMA_DIST_PCT = 12.0
    BASE_COOLDOWN = 600
    NOTIFY_INTERVAL = "10m"
    STATE_FILE = "bot_state.json"
    RSI_PERIOD = 14
    RSI_LATE_THR = 72
    ADX_PERIOD = 14
    ADX_TREND_THR = 22
    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9
    VOL_GATE = 1.1
    VOL_IDEAL = 1.4
    SCORE_ENTRY_THR = 60
    SL_ATR_MULT = 2.0
    TP_R_MULTIPLES = [1.5, 2.5, 4.0]
    EXCHANGE = "KuCoin"
    FETCH_RETRY = 3

def utc_now():
    return datetime.now(timezone.utc)

def _is_dead_zone(h):
    return False  # Crypto runs 24/7

def _rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def _macd(series, f=12, s=26, sig=9):
    fast = series.ewm(span=f, adjust=False).mean()
    slow = series.ewm(span=s, adjust=False).mean()
    line = fast - slow
    signal = line.ewm(span=sig, adjust=False).mean()
    return line, signal, line - signal

def _atr(df, p=14):
    tr = pd.concat([
        df["high"] - df["low"],
        abs(df["high"] - df["close"].shift(1)),
        abs(df["low"] - df["close"].shift(1))
    ], axis=1).max(axis=1)
    return tr.rolling(p).mean()

def _adx(df, p=14):
    """Proper ADX calculation — was using a broken proxy before."""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        abs(high - close.shift(1)),
        abs(low - close.shift(1))
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/p, adjust=False).mean()

    dm_plus  = high.diff()
    dm_minus = -low.diff()
    dm_plus  = dm_plus.where((dm_plus > dm_minus) & (dm_plus > 0), 0)
    dm_minus = dm_minus.where((dm_minus > dm_plus) & (dm_minus > 0), 0)

    di_plus  = 100 * dm_plus.ewm(alpha=1/p, adjust=False).mean() / atr.replace(0, np.nan)
    di_minus = 100 * dm_minus.ewm(alpha=1/p, adjust=False).mean() / atr.replace(0, np.nan)
    dx       = 100 * abs(di_plus - di_minus) / (di_plus + di_minus).replace(0, np.nan)
    adx      = dx.ewm(alpha=1/p, adjust=False).mean()
    return adx

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
            json.dump(self.data, f)

    def is_on_cooldown(self, s):
        return time.time() < self.data["cooldowns"].get(s, 0)

    def record_signal(self, s):
        self.data["cooldowns"][s] = time.time() + Config.BASE_COOLDOWN

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
                    json={
                        "chat_id": Config.TELEGRAM_CHAT_ID,
                        "text": msg,
                        # FIX 3: Switch to HTML parse mode — MarkdownV2 needs heavy escaping.
                        # "Markdown" v1 silently drops \uXXXX unicode escapes sent as literals.
                        "parse_mode": "HTML"
                    },
                    timeout=15
                )
                if r.status_code == 200:
                    return True
                else:
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
                    "⚠️ <b>GoatXX fetch_tickers FAILED</b>\n"
                    f"Error: <code>{str(e)[:200]}</code>\nBot stopped."
                )
                return None

def compute_goat_score(df_l, df_h, symbol):
    """
    FIX 4: Rewritten scoring. Previously:
      - score was always the same hardcoded expression (~72.9) regardless of data
      - RSI HTF filter was inverted (killed bounces instead of protecting them)
      - ADX was computed as abs(diff(14)).rolling(14).mean() / atr — not real ADX
      - EMA distance and ATR floors were too aggressive for ranging crypto markets
    """
    if len(df_l) < 50 or len(df_h) < 50:
        return None

    c   = df_l["close"].iloc[-1]
    o   = df_l["open"].iloc[-1]
    e50 = df_l["close"].ewm(span=50, adjust=False).mean().iloc[-1]

    # EMA distance filter — relaxed to 12%
    dist = abs(c - e50) / e50 * 100
    if dist > Config.MAX_EMA_DIST_PCT:
        return None

    # ATR filter — relaxed floors
    atr_v = _atr(df_l).iloc[-1]
    atr_p = (atr_v / c) * 100
    floor = Config.ATR_FLOOR_BTC if "BTC" in symbol else Config.ATR_FLOOR_ALT
    if atr_p < floor:
        return None

    # Volume gate
    v_ma = df_l["volume"].rolling(20).mean().iloc[-1]
    rv   = df_l["volume"].iloc[-1] / v_ma if v_ma > 0 else 0
    if rv < Config.VOL_GATE:
        return None

    # RSI late-stage filter (both short and standard RSI must be hot)
    rsi_val  = _rsi(df_l["close"]).iloc[-1]
    rsi7_val = _rsi(df_l["close"], 7).iloc[-1]
    if rsi_val >= Config.RSI_LATE_THR and rsi7_val >= Config.RSI_LATE_THR:
        return None

    # MACD
    _, _, hist = _macd(df_l["close"])
    h, hp = hist.iloc[-1], hist.iloc[-2]

    # Real ADX
    adx_val = _adx(df_l).iloc[-1]

    # HTF context
    ht_c   = df_h["close"].iloc[-1]
    ht_e50 = df_h["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    ht_e200= df_h["close"].ewm(span=200, adjust=False).mean().iloc[-1]
    htf_rsi= _rsi(df_h["close"]).iloc[-1]

    ht_t = (
        "BULLISH" if ht_c > ht_e50 > ht_e200 else
        "BEARISH" if ht_c < ht_e50 < ht_e200 else
        "NEUTRAL"
    )

    # FIX 5: Inverted HTF RSI filter corrected.
    # Old code: `if _rsi(df_h["close"]).iloc[-1] < 35 and c > o: return None`
    # This killed bullish bounces from oversold — the most reliable reversal setup.
    # New: only block if HTF RSI is overbought (>75) on a long entry, or oversold (<25) on short.
    is_long  = ht_t in ("BULLISH", "NEUTRAL") and c > e50 and c > o and h > 0 and h > hp
    is_short = ht_t in ("BEARISH", "NEUTRAL") and c < e50 and c < o and h < 0 and h < hp

    if is_long  and htf_rsi > 75:
        return None  # 1H overbought on a long — skip
    if is_short and htf_rsi < 25:
        return None  # 1H oversold on a short — skip

    if not (is_long or is_short):
        return None

    side  = "LONG" if is_long else "SHORT"

    # FIX 6: Real score built from actual indicator values, not hardcoded constants
    score = 50.0

    # Trend alignment (25 pts)
    score += 25.0 if ht_t == ("BULLISH" if is_long else "BEARISH") else 10.0

    # Momentum (20 pts)
    macd_bull = h > 0 and h > hp
    macd_bear = h < 0 and h < hp
    if (is_long and macd_bull) or (is_short and macd_bear):
        score += 20.0
    elif (is_long and h > 0) or (is_short and h < 0):
        score += 8.0

    # RSI zone (15 pts)
    if is_long  and 45 <= rsi_val <= 65:
        score += 15.0
    elif is_short and 35 <= rsi_val <= 55:
        score += 15.0
    elif is_long  and rsi_val < 45:
        score += 8.0   # oversold bounce bonus

    # Volume (15 pts)
    if rv >= Config.VOL_IDEAL:
        score += 15.0
    elif rv >= Config.VOL_GATE:
        score += 7.0

    # ADX trend strength (10 pts)
    if adx_val >= 30:
        score += 10.0
    elif adx_val >= Config.ADX_TREND_THR:
        score += 5.0

    # EMA proximity bonus (5 pts — price near EMA is clean entry)
    if dist < 2.0:
        score += 5.0

    # Effective score with timing penalties
    eff = score
    if rv   < Config.VOL_IDEAL:
        eff -= 8.0
    if adx_val < Config.ADX_TREND_THR:
        eff -= 8.0
    if dist > 6.0:
        eff -= 5.0

    if eff < Config.SCORE_ENTRY_THR:
        return None

    # Signal style
    brkout_hi = df_l["high"].rolling(50).max().iloc[-2] if len(df_l) >= 52 else None
    if dist < 1.5:
        style = "PRIME"
    elif brkout_hi is not None and c > brkout_hi:
        style = "BRK"
    else:
        style = "MOMENTUM"

    # SL / TP
    sl_dist = atr_v * Config.SL_ATR_MULT
    sl_price = (c - sl_dist) if is_long else (c + sl_dist)
    tp_prices = [
        (c + sl_dist * r) if is_long else (c - sl_dist * r)
        for r in Config.TP_R_MULTIPLES
    ]

    return {
        "side": side,
        "style": style,
        "entry": c,
        "sl": sl_price,
        "tp": tp_prices,
        "eff": round(eff, 1),
        "adx": round(adx_val, 1),
        "rsi": round(rsi_val, 1),
        "rv": round(rv, 2),
        "htf": ht_t,
    }

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

    # FIX 3: Use real Unicode characters — not escaped \uXXXX strings.
    # In Python source files, emoji are first-class string characters.
    nt.send(
        f"🤖 <b>GoatXX Scan Started</b>{nl}"
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

    signals_sent = 0
    scan_errors  = 0

    for s in syms:
        if state.is_on_cooldown(s):
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

            sig = compute_goat_score(df_l, df_h, s)
            if sig:
                m = (
                    f"🚀 <b>{sig['side']} SIGNAL</b>{nl}"
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

                if nt.send(m):
                    state.record_signal(s)
                    signals_sent += 1

        except Exception as e:
            scan_errors += 1
            print(f"Error scanning {s}: {e}")
            continue

    state.save()
    nt.send(
        f"✅ <b>Scan Complete</b>{nl}"
        f"Scanned: {len(syms)} pairs{nl}"
        f"Signals Sent: {signals_sent}{nl}"
        f"Errors: {scan_errors}"
    )

if __name__ == "__main__":
    main()
