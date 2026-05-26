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
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
    MIN_24H_VOLUME_USD = 5_000_000
    MAX_COINS_TO_SCAN  = 60
    QUOTE_CURRENCY     = "USDT"
    LTF_TIMEFRAME      = "5m"
    HTF_TIMEFRAME      = "1h"
    OHLCV_LIMIT        = 500
    ATR_FLOOR_BTC      = 0.2
    ATR_FLOOR_ALT      = 0.4
    MAX_EMA_DIST_PCT   = 8.0
    BASE_COOLDOWN      = 300
    STATE_FILE         = "bot_state.json"
    RSI_PERIOD         = 14
    RSI_LATE_THR       = 72
    ADX_PERIOD         = 14
    ADX_TREND_THR      = 22
    MACD_FAST          = 12
    MACD_SLOW          = 26
    MACD_SIGNAL        = 9
    VOL_GATE           = 1.1
    VOL_IDEAL          = 1.4
    SCORE_ENTRY_THR    = 60
    SL_ATR_MULT        = 2.0
    TP_R_MULTIPLES     = [1.5, 2.5, 4.0]
    EXCHANGE          = "KuCoin"
    FETCH_RETRY        = 3

def utc_now(): return datetime.now(timezone.utc)
def _is_dead_zone(h): return False  # Dead zone disabled - crypto runs 24/7

def _rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
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
    tr = pd.concat([df["high"]-df["low"], abs(df["high"]-df["close"].shift(1)), abs(df["low"]-df["close"].shift(1))], axis=1).max(axis=1)
    return tr.rolling(p).mean()

class BotState:
    def __init__(self, path):
        self.path = path
        self.data = {"cooldowns": {}}
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    self.data = json.load(f)
            except:
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
                r = requests.post(url, json={"chat_id": Config.TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
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
            tickers = ex.fetch_tickers()
            return tickers
        except Exception as e:
            err = traceback.format_exc()
            print(f"fetch_tickers attempt {attempt+1} failed: {e}")
            if attempt < Config.FETCH_RETRY - 1:
                time.sleep(5)
            else:
                notifier.send(f"\u26a0\ufe0f *GoatXX fetch_tickers FAILED*\nError: `{str(e)[:200]}`\nBot stopped.")
                return None

def compute_goat_score(df_l, df_h, symbol):
    if len(df_l) < 50 or len(df_h) < 50:
        return None
    c, o = df_l["close"].iloc[-1], df_l["open"].iloc[-1]
    e50 = df_l["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    dist = abs(c - e50) / e50 * 100
    if dist > Config.MAX_EMA_DIST_PCT:
        return None
    atr_v = _atr(df_l).iloc[-1]
    atr_p = (atr_v / c) * 100
    if atr_p < (Config.ATR_FLOOR_BTC if "BTC" in symbol else Config.ATR_FLOOR_ALT):
        return None
    adx_v = abs(df_l["close"].diff(14)).rolling(14).mean().iloc[-1] / atr_v
    v_ma = df_l["volume"].rolling(20).mean().iloc[-1]
    rv = df_l["volume"].iloc[-1] / v_ma
    if rv < Config.VOL_GATE:
        return None
    if _rsi(df_l["close"]).iloc[-1] >= Config.RSI_LATE_THR and _rsi(df_l["close"], 7).iloc[-1] >= Config.RSI_LATE_THR:
        return None
    _, _, hist = _macd(df_l["close"])
    h, hp = hist.iloc[-1], hist.iloc[-2]
    ht_c = df_h["close"].iloc[-1]
    ht_e50 = df_h["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    ht_e200 = df_h["close"].ewm(span=200, adjust=False).mean().iloc[-1]
    ht_t = "BULLISH" if ht_c > ht_e50 > ht_e200 else ("BEARISH" if ht_c < ht_e50 < ht_e200 else "NEUTRAL")
    if _rsi(df_h["close"]).iloc[-1] < 35.0 and c > o:
        return None
    is_l = ht_t in ("BULLISH", "NEUTRAL") and c > e50 and c > o and h > 0 and h > hp
    is_s = ht_t in ("BEARISH", "NEUTRAL") and c < e50 and c < o and h < 0 and h < hp
    if not (is_l or is_s):
        return None
    side = "LONG" if is_l else "SHORT"
    style = "PRIME" if dist < 1.0 else ("BRK" if c > df_l["high"].rolling(50).max().iloc[-2] else "MOMENTUM")
    score = (1400 + 1600 + 500 + (1800 if rv > 1.3 else 1200) + 500 + 250) / 85
    eff = score - (0 if rv > 1.2 else 12) - (0 if adx_v > 22 else 8)
    if eff < Config.SCORE_ENTRY_THR:
        return None
    sl = atr_v * Config.SL_ATR_MULT
    tp_p = [c + sl * r if is_l else c - sl * r for r in Config.TP_R_MULTIPLES]
    return {"side": side, "style": style, "entry": c, "sl": c-sl if is_l else c+sl, "tp": tp_p, "eff": eff}

def main():
    ex = ccxt.kucoin({"enableRateLimit": True})
    state = BotState(Config.STATE_FILE)
    nt = TelegramNotifier()
    nl = chr(10)

    # Fetch tickers with retry + error notification
    tickers = fetch_tickers_with_retry(ex, nt)
    if tickers is None:
        print("ERROR: Could not fetch tickers after retries. Exiting.")
        return

    syms = sorted(
        [s for s, d in tickers.items() if s.endswith("/USDT") and d.get("quoteVolume", 0) >= Config.MIN_24H_VOLUME_USD],
        key=lambda x: tickers[x]["quoteVolume"],
        reverse=True
    )[:Config.MAX_COINS_TO_SCAN]

    h = utc_now().hour
    session = 'DEAD' if _is_dead_zone(h) else 'ACTIVE'

    nt.send(
        f"\U0001f916 *GoatXX Scan Started*{nl}"
        f"Exchange: {Config.EXCHANGE}{nl}"
        f"Pairs Found: {len(syms)}{nl}"
        f"Interval: {Config.LTF_TIMEFRAME}{nl}"
        f"Session: {session}"
    )

    if _is_dead_zone(h):
        nt.send(f"\U0001f634 Dead zone ({h}:00 UTC) - skipping scan.")
        return

    if len(syms) == 0:
        nt.send(f"\u26a0\ufe0f No pairs found above volume threshold! Check exchange connectivity.")
        return

    signals_sent = 0
    scan_errors = 0
    for s in syms:
        if state.is_on_cooldown(s):
            continue
        try:
            df_l = pd.DataFrame(ex.fetch_ohlcv(s, "5m", limit=100), columns=["t","open","high","low","close","volume"])
            df_h = pd.DataFrame(ex.fetch_ohlcv(s, "1h", limit=100), columns=["t","open","high","low","close","volume"])
            sig = compute_goat_score(df_l, df_h, s)
            if sig:
                m = (
                    f"\U0001f680 *{sig['side']} SIGNAL*{nl}"
                    f"Pair: `{s}`{nl}"
                    f"Style: {sig['style']}{nl}"
                    f"Score: {sig['eff']:.1f}{nl}"
                    f"Entry: `{sig['entry']:.8f}`{nl}"
                    f"SL: `{sig['sl']:.8f}`"
                )
                for i, p in enumerate(sig['tp']):
                    m += f"{nl}TP{i+1}: `{p:.8f}`"
                if nt.send(m):
                    state.record_signal(s)
                    signals_sent += 1
        except Exception as e:
            scan_errors += 1
            print(f"Error scanning {s}: {e}")
            continue

    state.save()
    nt.send(
        f"\u2705 *Scan Complete*{nl}"
        f"Scanned: {len(syms)} pairs{nl}"
        f"Signals Sent: {signals_sent}{nl}"
        f"Errors: {scan_errors}{nl}"
        f"Duration: done"
    )

if __name__ == "__main__":
    main()
