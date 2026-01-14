#!/usr/bin/env python3
# Crypto Signal Bot - GitHub Actions Compatible (Long+Short, 15m schedule-safe)
# - Entries: 1h
# - Trend filter: 4h
# - Runs every 15 minutes but only processes when a new 1h candle has closed (persisted state)

import os
import json
import time
from datetime import datetime, timezone

import ccxt
import pandas as pd
import numpy as np
import requests


# ----------------------------
# Config
# ----------------------------
class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # Pairs
    SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "DOGE/USDT"]

    # Candle settings
    LTF_TIMEFRAME = "1h"   # entry timeframe
    HTF_TIMEFRAME = "4h"   # trend timeframe (keeps noise down)
    OHLCV_LIMIT = 220      # a bit > 200 to be safe with EMA/ATR warmup

    # Strategy parameters
    SIGNAL_COOLDOWN = 3600  # seconds per symbol (extra protection)

    ATR_SHORT = 14
    ATR_LONG = 100
    ATR_EXPANSION_RATIO = 1.3

    EXTREME_MOVE_3D_PCT = 30  # block long after +30% in 3d, block short after -30% in 3d

    # Risk model
    BTC_ATR_MULT = 1.5
    ALT_ATR_MULT = 2.0

    # Scheduling / state
    STATE_FILE = "bot_state.json"
    # If you run on 15m schedule, use a small delay (cron minute 5/20/35/50) so the last closed candle is stable.


def utc_now():
    return datetime.now(timezone.utc)


# ----------------------------
# Persistent State (for GitHub Actions)
# ----------------------------
class BotState:
    """
    Stores:
      - last_processed_ltf_ts: per-symbol last processed 1h candle timestamp (ms)
      - last_signal_ts: per-symbol unix time() for cooldown
    """
    def __init__(self, path):
        self.path = path
        self.data = {
            "last_processed_ltf_ts": {},
            "last_signal_ts": {},
        }
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            # ensure keys exist
            self.data.setdefault("last_processed_ltf_ts", {})
            self.data.setdefault("last_signal_ts", {})
        except Exception:
            # if state corrupt, start fresh
            self.data = {"last_processed_ltf_ts": {}, "last_signal_ts": {}}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)

    def get_last_candle_ts(self, symbol):
        return int(self.data["last_processed_ltf_ts"].get(symbol, 0))

    def set_last_candle_ts(self, symbol, ts_ms):
        self.data["last_processed_ltf_ts"][symbol] = int(ts_ms)

    def get_last_signal_time(self, symbol):
        return float(self.data["last_signal_ts"].get(symbol, 0.0))

    def set_last_signal_time(self, symbol, t):
        self.data["last_signal_ts"][symbol] = float(t)


# ----------------------------
# Engine
# ----------------------------
class CryptoEngine:
    def __init__(self):
        print("CRYPTO ENGINE SIGNAL BOT")
        print("Exchange: Kraken")
        self.exchange = ccxt.kraken({"enableRateLimit": True})
        self.exchange.load_markets()  # markets/symbols are only available after this [web:22]
        print("Exchange initialized and markets loaded.")

    @staticmethod
    def _to_ohlcv_df(ohlcv):
        # CCXT OHLCV format: [timestamp, open, high, low, close, volume]
        df = pd.DataFrame(ohlcv, columns=["t", "open", "high", "low", "close", "volume"])
        return df

    def fetch_ohlcv_df(self, symbol, timeframe, limit):
        # Unified signature: fetch_ohlcv(symbol, timeframe, since, limit, params) [web:8]
        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, None, limit)
        return self._to_ohlcv_df(ohlcv)

    @staticmethod
    def calculate_atr(df, period):
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def detect_regime(self, df_1h):
        atr_s = self.calculate_atr(df_1h, Config.ATR_SHORT)
        atr_l = self.calculate_atr(df_1h, Config.ATR_LONG)

        atr_s_last = float(atr_s.iloc[-1]) if len(atr_s) else 0.0
        atr_l_last = float(atr_l.iloc[-1]) if len(atr_l) else 0.0

        ratio = (atr_s_last / atr_l_last) if (atr_l_last and np.isfinite(atr_l_last)) else 0.0

        # 3-day move on 1h candles = 72 bars
        move_3d = 0.0
        if len(df_1h) > 72:
            base = float(df_1h["close"].iloc[-72])
            last = float(df_1h["close"].iloc[-1])
            if base != 0 and np.isfinite(base) and np.isfinite(last):
                move_3d = ((last - base) / base) * 100.0

        expanding = ratio > Config.ATR_EXPANSION_RATIO
        allow_long = expanding and not (move_3d > Config.EXTREME_MOVE_3D_PCT)
        allow_short = expanding and not (move_3d < -Config.EXTREME_MOVE_3D_PCT)

        return {
            "state": "EXPANSION" if expanding else "CONTRACTION",
            "atr_ratio": float(ratio),
            "atr_short": float(atr_s_last),
            "move_3d": float(move_3d),
            "allow_long": bool(allow_long),
            "allow_short": bool(allow_short),
            "trading_allowed": bool(allow_long or allow_short),
        }

    @staticmethod
    def analyze_trend(df_htf):
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
    def detect_entry(df_1h, trend):
        ema50 = df_1h["close"].ewm(span=50).mean()
        e50 = ema50.iloc[-1]

        last_open = df_1h["open"].iloc[-1]
        last_close = df_1h["close"].iloc[-1]
        last_low = df_1h["low"].iloc[-1]
        last_high = df_1h["high"].iloc[-1]

        vol_ma20 = df_1h["volume"].rolling(20).mean().iloc[-1]
        vol_ok = df_1h["volume"].iloc[-1] > (vol_ma20 * 1.2) if np.isfinite(vol_ma20) else False

        # LONG: wick below EMA50 + close above + green candle
        long_bounce = (last_low <= e50) and (last_close > e50) and (last_close > last_open)

        # SHORT: wick above EMA50 + close below + red candle
        short_reject = (last_high >= e50) and (last_close < e50) and (last_close < last_open)

        if trend == "BULLISH":
            quality = 2 + (1 if last_close > e50 else 0) + (1 if vol_ok else 0)
            if long_bounce and quality >= 3:
                return {
                    "signal": "LONG",
                    "pattern": "EMA Bounce",
                    "quality": int(quality),
                    "confidence": "HIGH" if quality >= 4 else "MEDIUM",
                }

        if trend == "BEARISH":
            quality = 2 + (1 if last_close < e50 else 0) + (1 if vol_ok else 0)
            if short_reject and quality >= 3:
                return {
                    "signal": "SHORT",
                    "pattern": "EMA Reject",
                    "quality": int(quality),
                    "confidence": "HIGH" if quality >= 4 else "MEDIUM",
                }

        return None

    @staticmethod
    def calculate_levels(symbol, entry, atr, side):
        is_btc = "BTC" in symbol
        mult = Config.BTC_ATR_MULT if is_btc else Config.ALT_ATR_MULT

        atr = float(atr) if atr and np.isfinite(atr) else 0.0
        entry = float(entry)

        if side == "LONG":
            sl = entry - (atr * mult)
            dist = entry - sl
            return {
                "entry": entry,
                "stop_loss": sl,
                "tp1": entry + dist * 1.5,
                "tp2": entry + dist * 2.5,
                "tp3": entry + dist * 4.0,
                "risk_pct": (dist / entry) * 100.0 if entry else 0.0,
            }

        # SHORT
        sl = entry + (atr * mult)
        dist = sl - entry
        return {
            "entry": entry,
            "stop_loss": sl,
            "tp1": entry - dist * 1.5,
            "tp2": entry - dist * 2.5,
            "tp3": entry - dist * 4.0,
            "risk_pct": (dist / entry) * 100.0 if entry else 0.0,
        }

    def scan_symbol(self, symbol, state: BotState):
        # sanity: ensure symbol exists on exchange
        if symbol not in self.exchange.symbols:
            print(f"[SKIP] {symbol} not in exchange symbols list.")
            return None

        # cooldown (persistent across runs)
        now = time.time()
        last_sig = state.get_last_signal_time(symbol)
        if last_sig and (now - last_sig) < Config.SIGNAL_COOLDOWN:
            mins = int((now - last_sig) / 60)
            print(f"[COOLDOWN] {symbol} last signal {mins} min ago.")
            return None

        # fetch candles
        df_1h = self.fetch_ohlcv_df(symbol, Config.LTF_TIMEFRAME, Config.OHLCV_LIMIT)
        df_htf = self.fetch_ohlcv_df(symbol, Config.HTF_TIMEFRAME, Config.OHLCV_LIMIT)

        if len(df_1h) < max(Config.ATR_LONG, 72, 60) or len(df_htf) < 210:
            print(f"[SKIP] {symbol} insufficient candles (1h={len(df_1h)} htf={len(df_htf)}).")
            return None

        # 15m schedule gate: only process when we see a NEW 1h candle timestamp
        last_candle_ts = int(df_1h["t"].iloc[-1])
        prev_ts = state.get_last_candle_ts(symbol)
        if prev_ts and last_candle_ts <= prev_ts:
            print(f"[GATE] {symbol} no new 1h candle.")
            return None
        state.set_last_candle_ts(symbol, last_candle_ts)

        regime = self.detect_regime(df_1h)
        if not regime["trading_allowed"]:
            print(f"[NO TRADE] {symbol} regime={regime['state']} atr_ratio={regime['atr_ratio']:.2f}")
            return None

        trend = self.analyze_trend(df_htf)
        entry_data = self.detect_entry(df_1h, trend)
        if not entry_data:
            print(f"[NO SETUP] {symbol} regime={regime['state']} trend={trend}")
            return None

        side = entry_data["signal"]
        if side == "LONG" and not regime["allow_long"]:
            print(f"[BLOCK] {symbol} long blocked (3d move {regime['move_3d']:.2f}%).")
            return None
        if side == "SHORT" and not regime["allow_short"]:
            print(f"[BLOCK] {symbol} short blocked (3d move {regime['move_3d']:.2f}%).")
            return None

        entry_price = float(df_1h["close"].iloc[-1])
        levels = self.calculate_levels(symbol, entry_price, regime["atr_short"], side)

        state.set_last_signal_time(symbol, now)

        return {
            "symbol": symbol,
            "signal": side,
            "pattern": entry_data["pattern"],
            "quality": entry_data["quality"],   # 3/4 or 4/4
            "confidence": entry_data["confidence"],
            "regime": regime,
            "htf_trend": trend,
            "levels": levels,
            "timestamp": utc_now(),
        }


# ----------------------------
# Telegram Notifier
# ----------------------------
class TelegramNotifier:
    def __init__(self):
        self.bot_token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage" if self.bot_token else ""

    def send_message(self, text):
        if not self.bot_token or not self.chat_id:
            print("[WARN] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. Skipping Telegram send.")
            return False

        try:
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
            # Telegram sendMessage supports parse_mode in the request body. [web:10]
            r = requests.post(self.base_url, data=payload, timeout=10)
            if r.status_code == 200:
                return True
            print(f"[TELEGRAM ERROR] {r.status_code} {r.text}")
            return False
        except Exception as e:
            print(f"[TELEGRAM EXCEPTION] {e}")
            return False

    @staticmethod
    def _fmt_price(x):
        # BTC needs fewer decimals than DOGE; this keeps it readable.
        if x >= 1000:
            return f"{x:.2f}"
        if x >= 1:
            return f"{x:.4f}"
        return f"{x:.6f}"

    def format_signal(self, s):
        l = s["levels"]
        side = s["signal"]

        if side == "LONG":
            title = "🟢 *LONG SIGNAL* 🟢"
            side_lbl = "BUY"
        else:
            title = "🔴 *SHORT SIGNAL* 🔴"
            side_lbl = "SELL"

        # show R labels aligned with direction (TPs already computed correctly)
        return (
            f"{title}\n\n"
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
            f"ATR Ratio: {s['regime']['atr_ratio']:.2f}\n"
            f"3D Move: {s['regime']['move_3d']:.2f}%\n"
            f"Time (UTC): {s['timestamp'].strftime('%H:%M:%S')}"
        )

    def send_signal(self, signal):
        return self.send_message(self.format_signal(signal))


# ----------------------------
# Main
# ----------------------------
def main():
    print("=" * 60)
    print("CRYPTO SIGNAL BOT - GITHUB ACTIONS (LONG+SHORT)")
    print(f"Time (UTC): {utc_now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Monitoring: {', '.join(Config.SYMBOLS)}")
    print("=" * 60)

    state = BotState(Config.STATE_FILE)
    engine = CryptoEngine()
    notifier = TelegramNotifier()

    notifier.send_message(
        "🤖 *Bot Scan Started*\n\n"
        "Exchange: Kraken\n"
        f"Pairs: {', '.join(Config.SYMBOLS)}\n"
        f"Schedule: every 15m (process new 1h candles only)\n"
        f"Time (UTC): {utc_now().strftime('%H:%M:%S')}\n"
    )

    signals = []
    for symbol in Config.SYMBOLS:
        s = engine.scan_symbol(symbol, state)
        if s:
            signals.append(s)
            notifier.send_signal(s)
            time.sleep(2)  # gentle pacing

    completion_msg = (
        f"✅ Scan complete: {len(signals)} signal(s) sent"
        if signals
        else "✅ Scan complete: No signals (filters not met)"
    )
    print(completion_msg)
    notifier.send_message(completion_msg)

    # Persist state for GitHub Actions (commit this file back, or store as artifact)
    state.save()

    print("=" * 60)
    print("BOT FINISHED")
    print("=" * 60)


if __name__ == "__main__":
    main()
