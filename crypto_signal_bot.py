#!/usr/bin/env python3
"""
crypto_signal_bot.py — Hermes Golden Ratio v13.0
Based on Nadh's Golden Ratio Profile framework.

Core Principles:
1. Low drawdown > High success rate (55% WR + 1% DD > 75% WR + 5% DD)
2. 8-day MA is the PRIMARY trend filter and breakout validator
3. Breakout validation: price must be close to 8-day MA for real breakout
4. Entry on retracement: breakout + pullback to 8-day MA
5. Band-based SL using Fibonacci Golden Ratio (0.618)
6. Exit only on candle close below band (not touch)
7. Repeatable > Spectacular
"""
import os, json, time
from datetime import datetime, timezone
from collections import defaultdict
import ccxt, pandas as pd, numpy as np, requests

# Fibonacci Golden Ratio
PHI = 1.618033988749895
PHI_INV = 0.618033988749895  # 1/PHI

class Config:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5515185305")
    EXCHANGE = "KuCoin"
    MIN_24H_VOLUME_USD = 3_000_000
    MAX_COINS_TO_SCAN = 80
    CAPITAL_PER_SIGNAL = 1000.0
    MAX_OPEN_PER_PAIR = 2
    MAX_OPEN_TOTAL = 10
    BASE_COOLDOWN = 300
    MIN_ENTRY_PRICE = 0.000001
    STATE_FILE = "bot_state.json"
    SIGNAL_LOG_FILE = "signals_log.json"
    BLOCKLIST = {"H/USDT"}
    # Risk management: tight SL, low drawdown focus
    MAX_LOSS_PCT = 1.0  # Max 1% loss per trade (investor: 3%)
    TP_R_MULTIPLES = [1.5, 2.5, 4.0]  # Let winners run

def utc_now():
    return datetime.now(timezone.utc)

# ── Indicators ──
def _rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def _sma(series, period):
    return series.rolling(window=period).mean()

def _atr(df, p=14):
    tr = pd.concat([df["high"] - df["low"], abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1))], axis=1).max(axis=1)
    return tr.rolling(p).mean()

def _bb(df, period=20, stds=2.0):
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    return mid - std * stds, mid, mid + std * stds

def _macd(series):
    fast, slow = series.ewm(span=12, adjust=False).mean(), series.ewm(span=26, adjust=False).mean()
    line = fast - slow
    signal = line.ewm(span=9, adjust=False).mean()
    return line, signal, line - signal

def _stochrsi(series):
    r = _rsi(series, 14)
    r_min = r.rolling(14).min()
    r_max = r.rolling(14).max()
    stoch = (r - r_min) / (r_max - r_min).replace(0, np.nan) * 100
    k = stoch.rolling(3).mean()
    d = k.rolling(3).mean()
    return k, d

def _cmf(df, period=20):
    mf_mult = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"]).replace(0, np.nan)
    mf_vol = mf_mult * df["volume"]
    return mf_vol.rolling(period).sum() / df["volume"].rolling(period).sum()

# Fibonacci Golden Ratio band calculation
def _fib_band(high, low, direction="long"):
    """Calculate support/resistance band using Fibonacci Golden Ratio."""
    range_val = high - low
    if direction == "long":
        # Support band: low to low + range * 0.618
        band_low = low
        band_high = low + range_val * PHI_INV
    else:
        # Resistance band: high - range * 0.618 to high
        band_low = high - range_val * PHI_INV
        band_high = high
    return band_low, band_high

# 8-day MA distance check (breakout validation)
def _ma8_distance(price, ma8):
    """Check if price is close to 8-day MA (for breakout validation)."""
    if ma8 == 0:
        return float('inf')
    return abs(price - ma8) / ma8 * 100  # Percentage distance

# ── Signal Logger ──
def _load_signals():
    if not os.path.exists(Config.SIGNAL_LOG_FILE): return []
    try:
        with open(Config.SIGNAL_LOG_FILE, encoding="utf-8") as f: return json.load(f)
    except: return []

def log_signal(symbol, sig):
    log = _load_signals()
    entry = {
        "id": f"SIG-{len(log)+1:04d}", "time": utc_now().isoformat(),
        "pair": symbol, "exchange": Config.EXCHANGE,
        "direction": sig["side"], "style": sig["style"],
        "score": sig.get("eff", 0),
        "rsi3": sig.get("rsi3", 0), "rsi14": sig.get("rsi14", 0),
        "ma8_dist": sig.get("ma8_dist", 0),
        "entry": sig["entry"], "sl": sig["sl"], "sl_band": sig.get("sl_band", []),
        "tp1": sig["tp"][0] if len(sig["tp"]) > 0 else None,
        "tp2": sig["tp"][1] if len(sig["tp"]) > 1 else None,
        "tp3": sig["tp"][2] if len(sig["tp"]) > 2 else None,
        "capital": Config.CAPITAL_PER_SIGNAL, "status": "OPEN",
        "filter_checked": True,  # Already filtered at bot level (time-of-day, RSI death zone, trend)
        "exit_price": None, "pnl_usd": None, "result": None,
        "closed_at": None, "exit_time": None,
    }
    log.append(entry)
    with open(Config.SIGNAL_LOG_FILE, "w", encoding="utf-8") as f: json.dump(log, f, indent=2)
    return entry["id"]

# ── Bot State ──
class BotState:
    def __init__(self, path):
        self.path = path
        self.data = {"cooldowns": {}}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f: self.data = json.load(f)
            except: pass
    def save(self):
        with open(self.path, "w", encoding="utf-8") as f: json.dump(self.data, f)
    def is_on_cooldown(self, s):
        return time.time() < self.data["cooldowns"].get(s, 0)
    def record_and_save(self, s):
        self.data["cooldowns"][s] = time.time() + Config.BASE_COOLDOWN
        self.save()

# ── Telegram ──
class TelegramNotifier:
    def send(self, msg):
        if not Config.TELEGRAM_BOT_TOKEN: return False
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
        for attempt in range(3):
            try:
                r = requests.post(url, json={"chat_id": Config.TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=15)
                if r.status_code == 200: return True
            except: pass
            time.sleep(2)
        return False

# Golden Ratio Signal Engine
def compute_signals(df_5m, df_15m, df_1h, df_4h, df_1d, symbol):
    if len(df_5m) < 50 or len(df_1h) < 50 or len(df_4h) < 50:
        return []
    try:
        c = df_5m["close"].iloc[-1]
        if c < Config.MIN_ENTRY_PRICE:
            return []
        
        # 8-day MA on 1H chart (PRIMARY filter)
        ma8_1h = _sma(df_1h["close"], 8).iloc[-1]
        ma8_1h_prev = _sma(df_1h["close"], 8).iloc[-2]
        c_1h = df_1h["close"].iloc[-1]
        ma8_dist = _ma8_distance(c_1h, ma8_1h)
        ma8_rising = ma8_1h > ma8_1h_prev
        ma8_falling = ma8_1h < ma8_1h_prev
        price_above_ma8 = c_1h > ma8_1h
        price_below_ma8 = c_1h < ma8_1h
        
        # Allow directions based on 8-day MA
        allow_long = price_above_ma8 and ma8_rising and ma8_dist < 3.0
        allow_short = price_below_ma8 and ma8_falling and ma8_dist < 3.0
        
        if not allow_long and not allow_short:
            return []
        
        # ── Time-of-day filter (built into bot) ──
        # Block entries at 10:00 UTC (historically 9.1% WR — manipulation hour)
        current_hour = utc_now().hour
        if current_hour == 10:
            return []
        
        # ── Indicators on 5m (needed for filters below) ──
        rsi3_5m = _rsi(df_5m["close"], 3).iloc[-1]
        rsi14_5m = _rsi(df_5m["close"], 14).iloc[-1]
        
        # ── RSI death zone filter (built into bot) ──
        # Block RSI 50-59 entries (historically 11.1% WR — overbought death zone)
        if rsi14_5m >= 50.0 and rsi14_5m <= 59.0:
            return []
        
        # ── Volume analysis ──
        v_ma = df_5m["volume"].rolling(20).mean().iloc[-1]
        rv = df_5m["volume"].iloc[-1] / v_ma if v_ma > 0 else 0
        
        # ── Multi-TF trend analysis (HTF) ──
        # 4h trend
        ma8_4h = _sma(df_4h["close"], 8).iloc[-1]
        c_4h = df_4h["close"].iloc[-1]
        trend_4h = "BULLISH" if c_4h > ma8_4h else "BEARISH"
        # 1d trend
        ma8_1d = _sma(df_1d["close"], 8).iloc[-1]
        c_1d = df_1d["close"].iloc[-1]
        trend_1d = "BULLISH" if c_1d > ma8_1d else "BEARISH"
        # Combined HTF
        if trend_4h == trend_1d:
            htf = trend_4h
        else:
            htf = "NEUTRAL"
        
        # ── Remaining indicators ──
        rsi3_15m = _rsi(df_15m["close"], 3).iloc[-1]
        rsi3_1h = _rsi(df_1h["close"], 3).iloc[-1]
        atr_5m = _atr(df_5m, 14).iloc[-1]
        atr_pct = (atr_5m / c) * 100 if c > 0 else 0
        _, _, macd_hist = _macd(df_5m["close"])
        mh = macd_hist.iloc[-1]
        srsi_k, srsi_d = _stochrsi(df_5m["close"])
        srsi_k, srsi_d = srsi_k.iloc[-1], srsi_d.iloc[-1]
        bbl, bbm, bbu = _bb(df_5m)
        bbl, bbm, bbu = bbl.iloc[-1], bbm.iloc[-1], bbu.iloc[-1]
        v_ma = df_5m["volume"].rolling(20).mean().iloc[-1]
        rv = df_5m["volume"].iloc[-1] / v_ma if v_ma > 0 else 0
        
        # Swing levels for band-based SL
        recent_low = df_5m["low"].rolling(20).min().iloc[-1]
        recent_high = df_5m["high"].rolling(20).max().iloc[-1]
        
        if atr_pct < 0.08:
            return []
        
        candidates = []
        
        # SIGNAL 1: Retracement Entry (Highest Probability)
        # Relaxed RSI-3 thresholds for realistic scalping
        if allow_long and ma8_dist < 2.0 and rsi3_5m < 30 and mh < 0:
            band_low, band_high = _fib_band(recent_high, recent_low, "long")
            sl = band_low
            sl_pct = (c - sl) / c * 100
            if sl_pct <= Config.MAX_LOSS_PCT and sl < c:
                tp = [c + (c - sl) * r for r in [1.5, 2.5, 4.0]]
                candidates.append({"side":"LONG","style":"NFI_RSI3_EXT","entry":c,"sl":sl,"tp":tp,
                    "eff":85,"rsi14":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),
                    "ma8_dist":round(ma8_dist,2),"band":[round(band_low,8),round(band_high,8)],
                    "htf":htf,"vol_x":round(rv,2)})
        
        if allow_short and ma8_dist < 2.0 and rsi3_5m > 70 and mh > 0:
            band_low, band_high = _fib_band(recent_high, recent_low, "short")
            sl = band_high
            sl_pct = (sl - c) / c * 100
            if sl_pct <= Config.MAX_LOSS_PCT and sl > c:
                tp = [c - (sl - c) * r for r in [1.5, 2.5, 4.0]]
                candidates.append({"side":"SHORT","style":"NFI_RSI3_EXT","entry":c,"sl":sl,"tp":tp,
                    "eff":85,"rsi14":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),
                    "ma8_dist":round(ma8_dist,2),"band":[round(band_low,8),round(band_high,8)],
                    "htf":htf,"vol_x":round(rv,2)})
        
        # SIGNAL 2: BB + RSI-3 (relaxed for scalping)
        if allow_long and ma8_dist < 2.5 and c <= bbl * 1.01 and rsi3_5m < 35:
            band_low, band_high = _fib_band(recent_high, recent_low, "long")
            sl = band_low
            sl_pct = (c - sl) / c * 100
            if sl_pct <= Config.MAX_LOSS_PCT and sl < c:
                tp = [c + (c - sl) * r for r in [1.5, 2.5, 4.0]]
                candidates.append({"side":"LONG","style":"NFI_BB_REV","entry":c,"sl":sl,"tp":tp,
                    "eff":78,"rsi14":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),
                    "ma8_dist":round(ma8_dist,2),"band":[round(band_low,8),round(band_high,8)],
                    "htf":htf,"vol_x":round(rv,2)})
        
        if allow_short and ma8_dist < 2.5 and c >= bbu * 0.99 and rsi3_5m > 65:
            band_low, band_high = _fib_band(recent_high, recent_low, "short")
            sl = band_high
            sl_pct = (sl - c) / c * 100
            if sl_pct <= Config.MAX_LOSS_PCT and sl > c:
                tp = [c - (sl - c) * r for r in [1.5, 2.5, 4.0]]
                candidates.append({"side":"SHORT","style":"NFI_BB_REV","entry":c,"sl":sl,"tp":tp,
                    "eff":78,"rsi14":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),
                    "ma8_dist":round(ma8_dist,2),"band":[round(band_low,8),round(band_high,8)],
                    "htf":htf,"vol_x":round(rv,2)})
        
        # SIGNAL 3: StochRSI (relaxed for scalping)
        if allow_long and ma8_dist < 2.5 and srsi_k > srsi_d and srsi_k < 30 and rsi3_5m < 40:
            band_low, band_high = _fib_band(recent_high, recent_low, "long")
            sl = band_low
            sl_pct = (c - sl) / c * 100
            if sl_pct <= Config.MAX_LOSS_PCT and sl < c:
                tp = [c + (c - sl) * r for r in [1.5, 2.5, 4.0]]
                candidates.append({"side":"LONG","style":"NFI_SRST","entry":c,"sl":sl,"tp":tp,
                    "eff":80,"rsi14":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),
                    "ma8_dist":round(ma8_dist,2),"band":[round(band_low,8),round(band_high,8)],
                    "htf":htf,"vol_x":round(rv,2)})
        
        if allow_short and ma8_dist < 2.5 and srsi_k < srsi_d and srsi_k > 70 and rsi3_5m > 60:
            band_low, band_high = _fib_band(recent_high, recent_low, "short")
            sl = band_high
            sl_pct = (sl - c) / c * 100
            if sl_pct <= Config.MAX_LOSS_PCT and sl > c:
                tp = [c - (sl - c) * r for r in [1.5, 2.5, 4.0]]
                candidates.append({"side":"SHORT","style":"NFI_SRST","entry":c,"sl":sl,"tp":tp,
                    "eff":80,"rsi14":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),
                    "ma8_dist":round(ma8_dist,2),"band":[round(band_low,8),round(band_high,8)],
                    "htf":htf,"vol_x":round(rv,2)})
        
        candidates.sort(key=lambda s: -s["eff"])
        return candidates[:2]
    except Exception as e:
        print(f"  [ERR] {symbol}: {e}")
        return []

def main():
    ex = ccxt.kucoin({"enableRateLimit": True})
    state = BotState(Config.STATE_FILE)
    nt = TelegramNotifier()
    
    tickers = ex.fetch_tickers()
    if tickers is None:
        print("ERROR: Could not fetch tickers")
        return
    
    syms = sorted(
        [s for s, d in tickers.items() if s.endswith("/USDT") and d.get("quoteVolume", 0) >= Config.MIN_24H_VOLUME_USD],
        key=lambda x: tickers[x]["quoteVolume"], reverse=True
    )[:Config.MAX_COINS_TO_SCAN]
    
    current_signals = _load_signals()
    total_open = sum(1 for s in current_signals if s.get("status") == "OPEN")
    signals_sent = 0
    style_counts = defaultdict(int)
    
    for s in syms:
        if s in Config.BLOCKLIST: continue
        if state.is_on_cooldown(s): continue
        pair_open = sum(1 for sig in current_signals if sig.get("pair") == s and sig.get("status") == "OPEN")
        if pair_open >= Config.MAX_OPEN_PER_PAIR: continue
        if total_open >= Config.MAX_OPEN_TOTAL: continue
        
        try:
            df_5m = pd.DataFrame(ex.fetch_ohlcv(s, "5m", limit=200), columns=["t","open","high","low","close","volume"])
            df_15m = pd.DataFrame(ex.fetch_ohlcv(s, "15m", limit=200), columns=["t","open","high","low","close","volume"])
            df_1h = pd.DataFrame(ex.fetch_ohlcv(s, "1h", limit=200), columns=["t","open","high","low","close","volume"])
            df_4h = pd.DataFrame(ex.fetch_ohlcv(s, "4h", limit=200), columns=["t","open","high","low","close","volume"])
            df_1d = pd.DataFrame(ex.fetch_ohlcv(s, "1d", limit=100), columns=["t","open","high","low","close","volume"])
            
            scalp_signals = compute_signals(df_5m, df_15m, df_1h, df_4h, df_1d, s)
            
            for sig in scalp_signals:
                log_signal(s, sig)
                state.record_and_save(s)
                total_open += 1
                m = (f"⚡ <b>{sig['side']} SCALP</b>\nPair: <code>{s}</code>\nStyle: {sig['style']}\n"
                     f"RSI-3: {sig.get('rsi3','N/A')} | RSI-14: {sig['rsi14']}\n"
                     f"HTF: {sig.get('htf','N/A')} | Vol: {sig.get('vol_x','N/A')}x\n"
                     f"MA8 Dist: {sig.get('ma8_dist','N/A')}%\n"
                     f"Entry: <code>{sig['entry']:.8f}</code>\nSL: <code>{sig['sl']:.8f}</code>")
                for i, p in enumerate(sig["tp"]):
                    m += f"\nTP{i+1}: <code>{p:.8f}</code>"
                nt.send(m)
                signals_sent += 1
                style_counts[sig["style"]] += 1
        except Exception as e:
            print(f"Error {s}: {e}")
            continue
    
    if signals_sent > 0:
        style_str = " | ".join(f"{k}: {v}" for k, v in sorted(style_counts.items(), key=lambda x: -x[1]))
        nt.send(f"✅ <b>Scan Complete</b>\nScanned: {len(syms)} | Signals: {signals_sent}\nStyles: {style_str}")

if __name__ == "__main__":
    main()
