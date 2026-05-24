#!/usr/bin/env python3
# GoatXX Enhanced Crypto Signal Bot
# Features: Adaptive Regime, GoatXX Quality Logic, Multi-Timeframe Trend
import os
import json
import time
from datetime import datetime, timezone
import ccxt
import pandas as pd
import numpy as np
import requests

class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "DOGE/USDT"]
    LTF_TIMEFRAME = "1h"
    HTF_TIMEFRAME = "4h"
    OHLCV_LIMIT = 300
    
    # Adaptive Logic
    ATR_FLOOR_BTC = 0.4
    ATR_FLOOR_ALT = 0.7
    MAX_EMA_DIST_PCT = 8.0
    BASE_COOLDOWN = 3600
    
    STATE_FILE = "bot_state.json"

def utc_now():
    return datetime.now(timezone.utc)

class BotState:
    def __init__(self, path: str):
        self.path = path
        self.data = {"last_processed_ltf_ts": {}, "last_signal_ts": {}}
        self.load()
    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    self.data = json.load(f)
            except: pass
    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f)
    def get_last_signal_time(self, sym): return self.data["last_signal_ts"].get(sym, 0)
    def set_last_signal_time(self, sym, t): self.data["last_signal_ts"][sym] = t

class GoatXXEngine:
    def __init__(self):
        self.exchange = ccxt.kraken({"enableRateLimit": True})
    
    def fetch_df(self, symbol, tf, limit):
        ohlcv = self.exchange.fetch_ohlcv(symbol, tf, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["t", "open", "high", "low", "close", "volume"])
        return df

    def analyze_regime(self, df, symbol):
        close = df["close"].iloc[-1]
        tr = pd.concat([df["high"]-df["low"], (df["high"]-df["close"].shift()).abs(), (df["low"]-df["close"].shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        atr_pct = (atr / close) * 100
        floor = Config.ATR_FLOOR_BTC if "BTC" in symbol else Config.ATR_FLOOR_ALT
        return {"ok": atr_pct > floor, "atr_pct": atr_pct}

    def get_trend(self, df_htf):
        e50 = df_htf["close"].ewm(span=50).mean().iloc[-1]
        e200 = df_htf["close"].ewm(span=200).mean().iloc[-1]
        c = df_htf["close"].iloc[-1]
        if c > e50 > e200: return "BULLISH"
        if c < e50 < e200: return "BEARISH"
        return "NEUTRAL"

    def detect_signal(self, df_ltf, trend):
        e50 = df_ltf["close"].ewm(span=50).mean().iloc[-1]
        c = df_ltf["close"].iloc[-1]
        dist = abs(c - e50) / e50 * 100
        if dist > Config.MAX_EMA_DIST_PCT: return None
        
        # GoatXX Quality Logic (Simplified)
        vol_ma = df_ltf["volume"].rolling(20).mean().iloc[-1]
        vol_ok = df_ltf["volume"].iloc[-1] > vol_ma * 1.1
        
        if trend == "BULLISH" and c > e50 and vol_ok:
            return {"side": "LONG", "quality": 3}
        if trend == "BEARISH" and c < e50 and vol_ok:
            return {"side": "SHORT", "quality": 3}
        return None

def main():
    engine = GoatXXEngine()
    state = BotState(Config.STATE_FILE)
    for sym in Config.SYMBOLS:
        df_ltf = engine.fetch_df(sym, Config.LTF_TIMEFRAME, Config.OHLCV_LIMIT)
        df_htf = engine.fetch_df(sym, Config.HTF_TIMEFRAME, Config.OHLCV_LIMIT)
        regime = engine.analyze_regime(df_ltf, sym)
        if not regime["ok"]: continue
        
        trend = engine.get_trend(df_htf)
        sig = engine.detect_signal(df_ltf, trend)
        if sig:
            print(f"SIGNAL: {sym} {sig['side']} Quality: {sig['quality']}")
            state.set_last_signal_time(sym, time.time())
    state.save()

if __name__ == '__main__': main()
