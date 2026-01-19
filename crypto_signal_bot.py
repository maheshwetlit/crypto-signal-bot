#!/usr/bin/env python3
# Crypto Signal Bot - IMPROVED VERSION (Momentum + Breakout Capable)
# - Entries: 1h
# - Trend filter: 4h
# - Runs every 15 minutes but only processes when a new 1h candle has closed
# 
# IMPROVEMENTS:
# - ATR floor (% of price) instead of expansion ratio gate
# - Displacement/breakout patterns added alongside bounce/reject
# - Extreme move filter now adjusts quality threshold instead of blocking
# - Dynamic cooldown based on volatility
# - More responsive to momentum moves

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
    OHLCV_LIMIT = 220

    ATR_FLOOR_BTC_PCT = 0.8
    ATR_FLOOR_ALT_PCT = 1.2
    EXTREME_MOVE_3D_PCT = 50
    BASE_COOLDOWN = 3600
    HIGH_VOL_COOLDOWN = 600
    HIGH_VOL_ATR_THRESHOLD = 1.5

    ATR_SHORT = 14
    ATR_LONG = 100
    BTC_ATR_MULT = 1.5
    ALT_ATR_MULT = 2.0
    STATE_FILE = "bot_state.json"


def utc_now():
    return datetime.now(timezone.utc)


class BotState:
    def __init__(self, path: str):
        self.path = path
        self.data = {"last_processed_ltf_ts": {}, "last_signal_ts": {}}
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            self.data.setdefault("last_processed_ltf_ts", {})
            self.data.setdefault("last_signal_ts", {})
        except Exception:
            self.data = {"last_processed_ltf_ts": {}, "last_signal_ts": {}}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)

    def get_last_candle_ts(self, symbol: str) -> int:
        return int(self.data["last_processed_ltf_ts"].get(symbol, 0))

    def set_last_candle_ts(self, symbol: str, ts_ms: int):
        self.data["last_processed_ltf_ts"][symbol] = int(ts_ms)

    def get_last_signal_time(self, symbol: str) -> float:
        return float(self.data["last_signal_ts"].get(symbol, 0.0))

    def set_last_signal_time(self, symbol: str, t: float):
        self.data["last_signal_ts"][symbol] = float(t)

class CryptoEngine:
    def __init__(self):
        print("CRYPTO ENGINE SIGNAL BOT - IMPROVED VERSION")
        print("Exchange: Kraken")
        self.exchange = ccxt.kraken({"enableRateLimit": True})
        self.exchange.load_markets()
        print("Exchange initialized and markets loaded.")

    @staticmethod
    def _to_ohlcv_df(ohlcv):
        return pd.DataFrame(ohlcv, columns=["t", "open", "high", "low", "close", "volume"])

    def fetch_ohlcv_df(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, None, limit)
        return self._to_ohlcv_df(ohlcv)

    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int) -> pd.Series:
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def detect_regime(self, df_1h: pd.DataFrame, symbol: str) -> dict:
        atr_s = self.calculate_atr(df_1h, Config.ATR_SHORT)
        atr_s_last = float(atr_s.iloc[-1]) if len(atr_s) else 0.0
        close = float(df_1h["close"].iloc[-1])
        atr_pct = (atr_s_last / close) * 100.0 if close > 0 else 0.0
        is_btc = "BTC" in symbol
        atr_floor = Config.ATR_FLOOR_BTC_PCT if is_btc else Config.ATR_FLOOR_ALT_PCT
        vol_ok = atr_pct > atr_floor
        move_3d = 0.0
        if len(df_1h) > 72:
            base = float(df_1h["close"].iloc[-72])
            if base != 0 and np.isfinite(base) and np.isfinite(close):
                move_3d = ((close - base) / base) * 100.0
        extreme_up = move_3d > Config.EXTREME_MOVE_3D_PCT
        extreme_down = move_3d < -Config.EXTREME_MOVE_3D_PCT
        require_quality = 4 if (extreme_up or extreme_down) else 3
        return {"state": "ACTIVE" if vol_ok else "LOW_VOL", "atr_pct": float(atr_pct), "atr_short": float(atr_s_last), "move_3d": float(move_3d), "vol_ok": bool(vol_ok), "extreme_up": bool(extreme_up), "extreme_down": bool(extreme_down), "require_quality": int(require_quality), "trading_allowed": bool(vol_ok)}

    @staticmethod
    def analyze_trend(df_htf: pd.DataFrame) -> str:
        ema50 = df_htf["close"].ewm(span=50).mean()
        ema200 = df_htf["close"].ewm(span=200).mean()
        close = df_htf["close"].iloc[-1]
        e50 = ema50.iloc[-1]
        e200 = ema200.iloc[-1]
        if close > e50 > e200:
            return "BULLISH"
        if close < e50 < e200:
            return "BEARISH"
        return "NEUTRAL"

    @staticmethod
    def detect_entry(df_1h: pd.DataFrame, trend: str, require_quality: int):
        ema50 = df_1h["close"].ewm(span=50).mean()
        e50 = ema50.iloc[-1]
        ema_slope = ema50.diff().iloc[-1]
        last_open = df_1h["open"].iloc[-1]
        last_close = df_1h["close"].iloc[-1]
        last_low = df_1h["low"].iloc[-1]
        last_high = df_1h["high"].iloc[-1]
        vol_ma20 = df_1h["volume"].rolling(20).mean().iloc[-1]
        vol_ok = df_1h["volume"].iloc[-1] > (vol_ma20 * 1.2) if np.isfinite(vol_ma20) else False
        long_bounce = (last_low <= e50) and (last_close > e50) and (last_close > last_open)
        short_reject = (last_high >= e50) and (last_close < e50) and (last_close < last_open)
        ema_dist_pct = abs((last_close - e50) / e50) * 100.0 if e50 > 0 else 0.0
        long_breakout = (last_close > e50 and ema_dist_pct > 0.3 and ema_slope > 0 and last_close > last_open)
        short_breakdown = (last_close < e50 and ema_dist_pct > 0.3 and ema_slope < 0 and last_close < last_open)
        if trend == "BULLISH":
            quality = 2
            pattern = None
            if long_bounce:
                pattern = "EMA Bounce"
                quality += (1 if last_close > e50 else 0) + (1 if vol_ok else 0)
            elif long_breakout:
                pattern = "Breakout"
                quality += (1 if ema_dist_pct > 0.5 else 0) + (1 if vol_ok else 0)
            if pattern and quality >= require_quality:
                return {"signal": "LONG", "pattern": pattern, "quality": int(quality), "confidence": "HIGH" if quality >= 4 else "MEDIUM"}
        if trend == "BEARISH":
            quality = 2
            pattern = None
            if short_reject:
                pattern = "EMA Reject"
                quality += (1 if last_close < e50 else 0) + (1 if vol_ok else 0)
            elif short_breakdown:
                pattern = "Breakdown"
                quality += (1 if ema_dist_pct > 0.5 else 0) + (1 if vol_ok else 0)
            if pattern and quality >= require_quality:
                return {"signal": "SHORT", "pattern": pattern, "quality": int(quality), "confidence": "HIGH" if quality >= 4 else "MEDIUM"}
        return None

    @staticmethod
    def calculate_levels(symbol: str, entry: float, atr: float, side: str) -> dict:
        is_btc = "BTC" in symbol
        mult = Config.BTC_ATR_MULT if is_btc else Config.ALT_ATR_MULT
        atr = float(atr) if atr and np.isfinite(atr) else 0.0
        entry = float(entry)
        if side == "LONG":
            sl = entry - (atr * mult)
            dist = entry - sl
            return {"entry": entry, "stop_loss": sl, "tp1": entry + dist * 1.5, "tp2": entry + dist * 2.5, "tp3": entry + dist * 4.0, "risk_pct": (dist / entry) * 100.0 if entry else 0.0}
        sl = entry + (atr * mult)
        dist = sl - entry
        return {"entry": entry, "stop_loss": sl, "tp1": entry - dist * 1.5, "tp2": entry - dist * 2.5, "tp3": entry - dist * 4.0, "risk_pct": (dist / entry) * 100.0 if entry else 0.0}

    def scan_symbol(self, symbol: str, state: BotState):
        if symbol not in self.exchange.symbols:
            print(f"[SKIP] {symbol} not in exchange symbols list.")
            return None
        now = time.time()
        df_1h = self.fetch_ohlcv_df(symbol, Config.LTF_TIMEFRAME, Config.OHLCV_LIMIT)
        df_htf = self.fetch_ohlcv_df(symbol, Config.HTF_TIMEFRAME, Config.OHLCV_LIMIT)
        if len(df_1h) < max(Config.ATR_LONG, 72, 60) or len(df_htf) < 210:
            print(f"[SKIP] {symbol} insufficient candles (1h={len(df_1h)} htf={len(df_htf)}).")
            return None
        regime = self.detect_regime(df_1h, symbol)
        cooldown = Config.BASE_COOLDOWN
        if regime["atr_pct"] > Config.HIGH_VOL_ATR_THRESHOLD:
            cooldown = Config.HIGH_VOL_COOLDOWN
        last_sig = state.get_last_signal_time(symbol)
        if last_sig and (now - last_sig) < cooldown:
            mins = int((now - last_sig) / 60)
            print(f"[COOLDOWN] {symbol} last signal {mins} min ago (cooldown={cooldown}s).")
            return None
        last_candle_ts = int(df_1h["t"].iloc[-1])
        prev_ts = state.get_last_candle_ts(symbol)
        if prev_ts and last_candle_ts <= prev_ts:
            print(f"[GATE] {symbol} no new 1h candle.")
            return None
        state.set_last_candle_ts(symbol, last_candle_ts)
        if not regime["trading_allowed"]:
            print(f"[NO TRADE] {symbol} regime={regime['state']} atr_pct={regime['atr_pct']:.2f}%")
            return None
        trend = self.analyze_trend(df_htf)
        entry_data = self.detect_entry(df_1h, trend, regime["require_quality"])
        if not entry_data:
            print(f"[NO SETUP] {symbol} regime={regime['state']} trend={trend} req_qual={regime['require_quality']}")
            return None
        entry_price = float(df_1h["close"].iloc[-1])
        levels = self.calculate_levels(symbol, entry_price, regime["atr_short"], entry_data["signal"])
        state.set_last_signal_time(symbol, now)
        return {"symbol": symbol, "signal": entry_data["signal"], "pattern": entry_data["pattern"], "quality": entry_data["quality"], "confidence": entry_data["confidence"], "regime": regime, "htf_trend": trend, "levels": levels, "timestamp": utc_now()}

class TelegramNotifier:
    def __init__(self):
        self.bot_token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage" if self.bot_token else ""

    def send_message(self, text: str) -> bool:
        if not self.bot_token or not self.chat_id:
            print("[WARN] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. Skipping Telegram send.")
            return False
        try:
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
            r = requests.post(self.base_url, data=payload, timeout=10)
            if r.status_code == 200:
                return True
            print(f"[TELEGRAM ERROR] {r.status_code} {r.text}")
            return False
        except Exception as e:
            print(f"[TELEGRAM EXCEPTION] {e}")
            return False

    @staticmethod
    def _fmt_price(x: float) -> str:
        if x >= 1000:
            return f"{x:.2f}"
        if x >= 1:
            return f"{x:.4f}"
        return f"{x:.6f}"

    def format_signal(self, s: dict) -> str:
        l = s["levels"]
        side = s["signal"]
        if side == "LONG":
            title = "🟢 *LONG SIGNAL* 🟢"
            side_lbl = "BUY"
        else:
            title = "🔴 *SHORT SIGNAL* 🔴"
            side_lbl = "SELL"
        return (f"{title}\n\n"
                f"💎 *Pair:* {s['symbol']}\n"
                f"📊 *Pattern:* {s['pattern']}\n"
                f"⭐ *Confidence:* {s['confidence']} ({s['quality']}/4)\n"
                f"🧭 *Side:* {side_lbl}\n\n"
                f"🎯 *Entry:* `{self._fmt_price(l['entry'])}`\n"
                f"🛡 *Stop Loss:* `{self._fmt_price(l['stop_loss'])}`\n"
                f"   *Risk:* {l['risk_pct']:.2f}%\n\n"
                f"💰 *Take Profits:*\n"
                f"   TP1: `{self._fmt_price(l['tp1'])}` (1.5R)\n"
                f"   TP2: `{self._fmt_price(l['tp2'])}` (2.5R)\n"
                f"   TP3: `{self._fmt_price(l['tp3'])}` (4.0R)\n\n"
                f"📈 *CONDITIONS*\n"
                f"Regime: {s['regime']['state']}\n"
                f"HTF Trend: {s['htf_trend']}\n"
                f"ATR%: {s['regime']['atr_pct']:.2f}%\n"
                f"3D Move: {s['regime']['move_3d']:.2f}%\n"
                f"Time (UTC): {s['timestamp'].strftime('%H:%M:%S')}")

    def send_signal(self, signal: dict) -> bool:
        return self.send_message(self.format_signal(signal))


def main():
    print("=" * 60)
    print("CRYPTO SIGNAL BOT - IMPROVED (MOMENTUM + BREAKOUT)")
    print(f"Time (UTC): {utc_now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Monitoring: {', '.join(Config.SYMBOLS)}")
    print("=" * 60)

    state = BotState(Config.STATE_FILE)
    engine = CryptoEngine()
    notifier = TelegramNotifier()

    notifier.send_message(
        "🤖 *Bot Scan Started* (IMPROVED)\n\n"
        "Exchange: Kraken\n"
        f"Pairs: {', '.join(Config.SYMBOLS)}\n"
        "Schedule: every 15m (process new 1h candles)\n"
        "Improvements: ATR floor, breakout patterns, dynamic cooldown\n"
        f"Time (UTC): {utc_now().strftime('%H:%M:%S')}\n"
    )

    signals = []
    for symbol in Config.SYMBOLS:
        s = engine.scan_symbol(symbol, state)
        if s:
            signals.append(s)
            notifier.send_signal(s)
            time.sleep(2)

    completion_msg = (f"✅ Scan complete: {len(signals)} signal(s) sent"
                     if signals
                     else "✅ Scan complete: No signals (filters not met)")
    print(completion_msg)
    notifier.send_message(completion_msg)

    state.save()

    print("=" * 60)
    print("BOT FINISHED")
    print("=" * 60)


if __name__ == "__main__":
    main()
