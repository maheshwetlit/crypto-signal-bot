#!/usr/bin/env python3
# GoatXX Enhanced Crypto Signal Bot
# Features: Dynamic Scanner ($50M+ Volume), Adaptive Regime, BRK/Prime Logic
# Exchange: Binance | Scan: 5m
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
    
    # Scanner Config
    MIN_24H_VOLUME_USD = 50_000_000
    MAX_COINS_TO_SCAN = 60
    QUOTE_CURRENCY = "USDT"
    
    LTF_TIMEFRAME = "5m"
    HTF_TIMEFRAME = "1h"
    OHLCV_LIMIT = 500
    
    # Adaptive Logic
    ATR_FLOOR_BTC = 0.2
    ATR_FLOOR_ALT = 0.4
    MAX_EMA_DIST_PCT = 5.0
    BASE_COOLDOWN = 300
    
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
        self.exchange = ccxt.binance({"enableRateLimit": True})
    
    def get_top_volume_symbols(self):
        """Dynamically fetch symbols with >$50M 24h volume"""
        try:
            tickers = self.exchange.fetch_tickers()
            filtered = []
            for symbol, data in tickers.items():
                if not symbol.endswith(Config.QUOTE_CURRENCY): continue
                vol = data.get('quoteVolume', 0)
                if vol >= Config.MIN_24H_VOLUME_USD:
                    filtered.append({'symbol': symbol, 'volume': vol})
            
            # Sort by volume and take top N
            filtered.sort(key=lambda x: x['volume'], reverse=True)
            top_symbols = [x['symbol'] for x in filtered[:Config.MAX_COINS_TO_SCAN]]
            print(f"Scanner: Found {len(top_symbols)} coins with >${Config.MIN_24H_VOLUME_USD/1e6}M volume.")
            return top_symbols
        except Exception as e:
            print(f"Scanner Error: {e}")
            return []

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
        ema50 = df_ltf["close"].ewm(span=50).mean()
        e50 = ema50.iloc[-1]
        c = df_ltf["close"].iloc[-1]
        o = df_ltf["open"].iloc[-1]
        dist = abs(c - e50) / e50 * 100
        if dist > Config.MAX_EMA_DIST_PCT: return None
        
        vol_ma = df_ltf["volume"].rolling(20).mean().iloc[-1]
        vol_ok = df_ltf["volume"].iloc[-1] > vol_ma * 1.2
        
        sig_type = None
        if trend == "BULLISH" and c > e50:
            if c > o and vol_ok:
                sig_type = "PRIME" if dist < 1.0 else "BRK"
                return {"side": "LONG", "type": sig_type}
        if trend == "BEARISH" and c < e50:
            if c < o and vol_ok:
                sig_type = "PRIME" if dist < 1.0 else "BRK"
                return {"side": "SHORT", "type": sig_type}
        return None

def main():
    engine = GoatXXEngine()
    state = BotState(Config.STATE_FILE)
    symbols = engine.get_top_volume_symbols()
    
    print(f"Bot Scan Started: {len(symbols)} pairs | {utc_now()}")
    for sym in symbols:
        try:
            df_ltf = engine.fetch_df(sym, Config.LTF_TIMEFRAME, Config.OHLCV_LIMIT)
            df_htf = engine.fetch_df(sym, Config.HTF_TIMEFRAME, Config.OHLCV_LIMIT)
            regime = engine.analyze_regime(df_ltf, sym)
            if not regime["ok"]: continue
            
            trend = engine.get_trend(df_htf)
            sig = engine.detect_signal(df_ltf, trend)
            if sig:
                msg = f"🚀 {sig['type']} SIGNAL: {sym} {sig['side']} | Trend: {trend}"
                print(msg)
                state.set_last_signal_time(sym, time.time())
        except Exception: continue
    state.save()

if __name__ == '__main__':
    while True:
        main()
        time.sleep(300)
