#!/usr/bin/env python3
import os, json, time
from datetime import datetime, timezone
from collections import defaultdict
import ccxt, pandas as pd, numpy as np, requests

class Config:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    TELEGRAM_BOT_TOKEN='8539744777:AAGf5_jFh0bNcp1zQONkmHERUPDsVvDxmKU'
    TELEGRAM_CHAT_ID = "5515185305"
    MIN_24H_VOLUME_USD = 3_000_000
    MAX_COINS_TO_SCAN = 80
    EXCHANGE = "KuCoin"
    CAPITAL_PER_SIGNAL = 1000.0
    MAX_OPEN_PER_PAIR = 2
    MAX_OPEN_TOTAL = 15
    BASE_COOLDOWN = 300
    MIN_ENTRY_PRICE = 0.000001
    SL_ATR_MULT = 1.5
    MAX_SL_PCT = 2.0
    MIN_SL_PCT = 0.3
    TP_R_MULTIPLES = [1.5, 2.0, 3.0]
    STATE_FILE = "bot_state.json"
    SIGNAL_LOG_FILE = "signals_log.json"
    BLOCKLIST = {"H/USDT"}

def utc_now():
    return datetime.now(timezone.utc)
def _rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def _atr(df, p=14):
    tr = pd.concat([df["high"] - df["low"], abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1))], axis=1).max(axis=1)
    return tr.rolling(p).mean()

def _bb(df):
    mid = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    return mid - 2*std, mid, mid + 2*std

def _aroon(df, period=14):
    high, low = df["high"], df["low"]
    up = high.rolling(period).apply(lambda x: (period - np.argmax(x)) / period * 100, raw=True)
    down = low.rolling(period).apply(lambda x: (period - np.argmin(x)) / period * 100, raw=True)
    return up, down

def _cmf(df, period=20):
    mf_mult = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"]).replace(0, np.nan)
    mf_vol = mf_mult * df["volume"]
    return mf_vol.rolling(period).sum() / df["volume"].rolling(period).sum()

def _macd(series):
    fast, slow = series.ewm(span=12, adjust=False).mean(), series.ewm(span=26, adjust=False).mean()
    line = fast - slow
    signal = line.ewm(span=9, adjust=False).mean()
    return line - signal

def _stochrsi(series):
    r = _rsi(series, 14)
    r_min = r.rolling(14).min()
    r_max = r.rolling(14).max()
    stoch = (r - r_min) / (r_max - r_min).replace(0, np.nan) * 100
    k = stoch.rolling(3).mean()
    d = k.rolling(3).mean()
    return k, d

def _load_signals():
    if not os.path.exists(Config.SIGNAL_LOG_FILE): return []
    try:
        with open(Config.SIGNAL_LOG_FILE) as f: return json.load(f)
    except: return []

def log_signal(symbol, sig):
    log = _load_signals()
    entry = {"id": f"SIG-{len(log)+1:04d}", "time": utc_now().isoformat(),
        "pair": symbol, "exchange": Config.EXCHANGE,
        "direction": sig["side"], "style": sig["style"],
        "score": sig.get("eff", 0), "adx": sig.get("adx", 0),
        "rsi": sig.get("rsi", 0), "rsi3": sig.get("rsi3", 0),
        "volume_x": sig.get("rv", 0), "htf_trend": sig.get("htf", ""),
        "entry": sig["entry"], "sl": sig["sl"],
        "tp1": sig["tp"][0] if len(sig["tp"]) > 0 else None,
        "tp2": sig["tp"][1] if len(sig["tp"]) > 1 else None,
        "tp3": sig["tp"][2] if len(sig["tp"]) > 2 else None,
        "tp_main": sig["tp"][-1] if sig["tp"] else None,
        "capital": Config.CAPITAL_PER_SIGNAL, "status": "OPEN",
        "exit_price": None, "pnl_usd": None, "result": None,
        "closed_at": None, "exit_time": None}
    log.append(entry)
    with open(Config.SIGNAL_LOG_FILE, "w") as f: json.dump(log, f, indent=2)
    return entry["id"]

class BotState:
    def __init__(self, path):
        self.path = path
        self.data = {"cooldowns": {}}
        if os.path.exists(path):
            try:
                with open(path) as f: self.data = json.load(f)
            except: pass
    def save(self):
        with open(self.path, "w") as f: json.dump(self.data, f)
    def is_on_cooldown(self, s):
        return time.time() < self.data["cooldowns"].get(s, 0)
    def record_and_save(self, s):
        self.data["cooldowns"][s] = time.time() + Config.BASE_COOLDOWN
        self.save()

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

def compute_signals(df_5m, df_15m, df_1h, df_4h, df_1d, symbol):
    if len(df_5m) < 50 or len(df_1h) < 50: return []
    try:
        c = df_5m["close"].iloc[-1]
        if c < Config.MIN_ENTRY_PRICE: return []

        rsi3_5m = _rsi(df_5m["close"], 3).iloc[-1]
        rsi3_15m = _rsi(df_15m["close"], 3).iloc[-1]
        rsi3_1h = _rsi(df_1h["close"], 3).iloc[-1]
        rsi14_5m = _rsi(df_5m["close"], 14).iloc[-1]
        atr_5m = _atr(df_5m, 14).iloc[-1]
        atr_pct = (atr_5m / c) * 100 if c > 0 else 0
        bbl, bbm, bbu = _bb(df_5m)
        bbl, bbm, bbu = bbl.iloc[-1], bbm.iloc[-1], bbu.iloc[-1]
        v_ma = df_5m["volume"].rolling(20).mean().iloc[-1]
        rv = df_5m["volume"].iloc[-1] / v_ma if v_ma > 0 else 0
        mh = _macd(df_5m["close"]).iloc[-1]
        srsi_k, srsi_d = _stochrsi(df_5m["close"])
        srsi_k, srsi_d = srsi_k.iloc[-1], srsi_d.iloc[-1]
        aroon_up, aroon_down = _aroon(df_5m, 14)
        aroon_osc = aroon_up.iloc[-1] - aroon_down.iloc[-1]
        cmf = _cmf(df_5m, 20).iloc[-1]
        ema50_1h = _ema(df_1h["close"], 50).iloc[-1]
        ema200_1h = _ema(df_1h["close"], 200).iloc[-1]
        c_1h = df_1h["close"].iloc[-1]
        ema50_4h = _ema(df_4h["close"], 50).iloc[-1]
        c_4h = df_4h["close"].iloc[-1]
        ema200_1d = _ema(df_1d["close"], 200).iloc[-1]
        c_1d = df_1d["close"].iloc[-1]

        trend_1h = "BULL" if c_1h > ema50_1h > ema200_1h else "BEAR" if c_1h < ema50_1h < ema200_1h else "NEUTRAL"
        trend_4h = "BULL" if c_4h > ema50_4h else "BEAR"
        trend_1d = "BULL" if c_1d > ema200_1d else "BEAR"

        bull_count = sum([1 for t in [trend_1h, trend_4h, trend_1d] if t == "BULL"])
        bear_count = sum([1 for t in [trend_1h, trend_4h, trend_1d] if t == "BEAR"])
        neutral_count = sum([1 for t in [trend_1h, trend_4h, trend_1d] if t == "NEUTRAL"])
        allow_long = (bull_count >= 2) or (bull_count >= 1 and neutral_count >= 2)
        allow_short = (bear_count >= 2) or (bear_count >= 1 and neutral_count >= 2)
        if trend_1h == "BEAR" and trend_4h == "BEAR" and trend_1d == "BEAR": allow_long = False
        if trend_1h == "BULL" and trend_4h == "BULL" and trend_1d == "BULL": allow_short = False

        if atr_pct < 0.08: return []
        candidates = []

        if allow_long and rsi3_5m < 10 and rsi3_15m < 20 and rsi3_1h < 30 and mh < 0 and srsi_k < 20:
            sl = min(c - atr_5m * Config.SL_ATR_MULT, c * 0.997)
            tp = [c + atr_5m * r for r in Config.TP_R_MULTIPLES]
            candidates.append({"side":"LONG","style":"NFI_RSI3_EXT","entry":c,"sl":sl,"tp":tp,
                "eff":80,"adx":0,"rsi":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),"rv":round(rv,2),"htf":trend_1h})
        if allow_short and rsi3_5m > 90 and rsi3_15m > 80 and rsi3_1h > 70 and mh > 0 and srsi_k > 80:
            sl = max(c + atr_5m * Config.SL_ATR_MULT, c * 1.003)
            tp = [c - atr_5m * r for r in Config.TP_R_MULTIPLES]
            candidates.append({"side":"SHORT","style":"NFI_RSI3_EXT","entry":c,"sl":sl,"tp":tp,
                "eff":80,"adx":0,"rsi":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),"rv":round(rv,2),"htf":trend_1h})

        if allow_long and c <= bbl * 1.01 and rsi3_5m < 15 and cmf > -0.1 and rsi3_1h < 40:
            sl = min(c - atr_5m * Config.SL_ATR_MULT, c * 0.997)
            tp = [c + atr_5m * r for r in Config.TP_R_MULTIPLES]
            candidates.append({"side":"LONG","style":"NFI_BB_REV","entry":c,"sl":sl,"tp":tp,
                "eff":75,"adx":0,"rsi":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),"rv":round(rv,2),"htf":trend_1h})
        if allow_short and c >= bbu * 0.99 and rsi3_5m > 85 and cmf < 0.1 and rsi3_1h > 60:
            sl = max(c + atr_5m * Config.SL_ATR_MULT, c * 1.003)
            tp = [c - atr_5m * r for r in Config.TP_R_MULTIPLES]
            candidates.append({"side":"SHORT","style":"NFI_BB_REV","entry":c,"sl":sl,"tp":tp,
                "eff":75,"adx":0,"rsi":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),"rv":round(rv,2),"htf":trend_1h})

        if allow_long and srsi_k > srsi_d and srsi_k < 25 and rsi3_5m < 20 and rsi3_15m < 30:
            sl = min(c - atr_5m * Config.SL_ATR_MULT, c * 0.997)
            tp = [c + atr_5m * r for r in Config.TP_R_MULTIPLES]
            candidates.append({"side":"LONG","style":"NFI_SRST","entry":c,"sl":sl,"tp":tp,
                "eff":78,"adx":0,"rsi":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),"rv":round(rv,2),"htf":trend_1h})
        if allow_short and srsi_k < srsi_d and srsi_k > 75 and rsi3_5m > 80 and rsi3_15m > 70:
            sl = max(c + atr_5m * Config.SL_ATR_MULT, c * 1.003)
            tp = [c - atr_5m * r for r in Config.TP_R_MULTIPLES]
            candidates.append({"side":"SHORT","style":"NFI_SRST","entry":c,"sl":sl,"tp":tp,
                "eff":78,"adx":0,"rsi":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),"rv":round(rv,2),"htf":trend_1h})

        if allow_long and cmf > 0 and aroon_osc > 20 and rsi3_5m < 25 and rsi3_1h < 40:
            sl = min(c - atr_5m * Config.SL_ATR_MULT, c * 0.997)
            tp = [c + atr_5m * r for r in Config.TP_R_MULTIPLES]
            candidates.append({"side":"LONG","style":"NFI_CMF","entry":c,"sl":sl,"tp":tp,
                "eff":76,"adx":0,"rsi":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),"rv":round(rv,2),"htf":trend_1h})
        if allow_short and cmf < 0 and aroon_osc < -20 and rsi3_5m > 75 and rsi3_1h > 60:
            sl = max(c + atr_5m * Config.SL_ATR_MULT, c * 1.003)
            tp = [c - atr_5m * r for r in Config.TP_R_MULTIPLES]
            candidates.append({"side":"SHORT","style":"NFI_CMF","entry":c,"sl":sl,"tp":tp,
                "eff":76,"adx":0,"rsi":round(rsi14_5m,1),"rsi3":round(rsi3_5m,1),"rv":round(rv,2),"htf":trend_1h})

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
                     f"RSI-3: {sig.get('rsi3','N/A')} | RSI-14: {sig['rsi']}\nHTF: {sig['htf']} | Vol: {sig['rv']}x\n"
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
