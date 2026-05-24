#!/usr/bin/env python3
# GoatXX Enhanced Crypto Signal Bot

import os
import json
import time
from datetime import datetime, timezone

import ccxt
import pandas as pd
import numpy as np
import requests


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class Config:
    TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")
    MIN_24H_VOLUME_USD   = 50_000_000
    MAX_COINS_TO_SCAN    = 60
    QUOTE_CURRENCY       = "USDT"
    LTF_TIMEFRAME        = "5m"
    HTF_TIMEFRAME        = "1h"
    OHLCV_LIMIT          = 500
    ATR_FLOOR_BTC        = 0.2
    ATR_FLOOR_ALT        = 0.4
    MAX_EMA_DIST_PCT     = 5.0
    BASE_COOLDOWN        = 300       # seconds between signals for same symbol
    STATE_FILE           = "bot_state.json"
    # Risk / reward
    SL_ATR_MULT          = 1.0       # stop = entry +/- SL_ATR_MULT * ATR
    TP_R_MULTIPLES       = [1.5, 2.5, 4.0]
    # Confidence scoring weights
    CONF_WEIGHTS = {
        "trend_aligned":   1,        # HTF trend matches side
        "vol_spike":       1,        # volume > 1.2x MA
        "prime_entry":     1,        # within 1 % of EMA50
        "regime_active":   1,        # ATR% above floor
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _3d_move_pct(df: pd.DataFrame) -> float:
    """Percentage price change over last ~3 days (864 x 5-min candles)."""
    lookback = min(864, len(df) - 1)
    old_close = df["close"].iloc[-(lookback + 1)]
    new_close = df["close"].iloc[-1]
    if old_close == 0:
        return 0.0
    return round((new_close - old_close) / old_close * 100, 2)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
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
            except Exception:
                pass

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f)

    def get_last_signal_time(self, sym: str) -> float:
        return self.data["last_signal_ts"].get(sym, 0)

    def set_last_signal_time(self, sym: str, t: float):
        self.data["last_signal_ts"][sym] = t

    def is_on_cooldown(self, sym: str) -> bool:
        return (time.time() - self.get_last_signal_time(sym)) < Config.BASE_COOLDOWN


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
class TelegramNotifier:
    def __init__(self):
        self.bot_token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id   = Config.TELEGRAM_CHAT_ID
        self.url = (
            "https://api.telegram.org/bot" + self.bot_token + "/sendMessage"
            if self.bot_token else ""
        )

    def send_message(self, text: str) -> bool:
        if not self.bot_token or not self.chat_id:
            print("[Telegram] No credentials — printing message:\n" + text)
            return False
        try:
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
            r = requests.post(self.url, data=payload, timeout=10)
            return r.status_code == 200
        except Exception as exc:
            print("[Telegram] send failed:", exc)
            return False


# ---------------------------------------------------------------------------
# Signal formatting
# ---------------------------------------------------------------------------
def format_signal_message(sig: dict) -> str:
    side_emoji = "\U0001f7e2" if sig["side"] == "LONG" else "\U0001f534"   # green / red circle
    side_label = "LONG" if sig["side"] == "LONG" else "SHORT"
    action     = "BUY"  if sig["side"] == "LONG" else "SELL"

    conf_score = sig["confidence"]["score"]
    conf_max   = sig["confidence"]["max"]
    conf_label = (
        "HIGH"   if conf_score >= 4 else
        "MEDIUM" if conf_score >= 3 else
        "LOW"
    )

    tp_lines = "\n".join(
        "TP" + str(i + 1) + ": " + "{:.6f}".format(tp["price"])
        + " (" + str(tp["r"]) + "R)"
        for i, tp in enumerate(sig["take_profits"])
    )

    msg = (
        side_emoji + " *" + side_label + " SIGNAL* " + side_emoji + "\n"
        "\U0001f48e Pair: `" + sig["symbol"] + "`\n"
        "\U0001f4ca Pattern: " + sig["pattern"] + "\n"
        "\U0001f91d Style: " + sig["style"] + "\n"
        "\u2b50 Confidence: " + conf_label
        + " (" + str(conf_score) + "/" + str(conf_max) + ")\n"
        "\U0001f9ed Side: " + action + "\n"
        "\U0001f3af Entry: `" + "{:.6f}".format(sig["entry"]) + "`\n"
        "\U0001f6e1 Stop Loss: `" + "{:.6f}".format(sig["stop_loss"]) + "`\n"
        "   Risk: " + "{:.2f}".format(sig["risk_pct"]) + "%\n"
        "\U0001f4b0 Take Profits:\n"
        + tp_lines + "\n"
        "\U0001f4dd *CONDITIONS*\n"
        "Regime: " + sig["regime"] + "\n"
        "HTF Trend: " + sig["trend"] + "\n"
        "ATR%: " + "{:.2f}".format(sig["atr_pct"]) + "%\n"
        "3D Move: " + ("{:+.2f}".format(sig["move_3d"])) + "%\n"
        "Time (UTC): " + sig["timestamp"]
    )
    return msg


def format_start_message(n_symbols: int) -> str:
    return (
        "\U0001f916 *GoatXX Scan Started*\n"
        "Exchange: Binance\n"
        "Pairs Found: " + str(n_symbols) + "\n"
        "Interval: " + Config.LTF_TIMEFRAME
    )


def format_summary_message(signals_sent: int, scanned: int, elapsed: float) -> str:
    return (
        "\u2705 *Scan Complete*\n"
        "Scanned: " + str(scanned) + " pairs\n"
        "Signals Sent: " + str(signals_sent) + "\n"
        "Duration: " + "{:.1f}".format(elapsed) + "s"
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class GoatXXEngine:
    def __init__(self):
        self.exchange = ccxt.binance({"enableRateLimit": True})

    # --- Market universe ---------------------------------------------------
    def get_top_volume_symbols(self) -> list:
        try:
            tickers = self.exchange.fetch_tickers()
            filtered = [
                {"symbol": sym, "volume": d.get("quoteVolume", 0)}
                for sym, d in tickers.items()
                if sym.endswith(Config.QUOTE_CURRENCY)
                and d.get("quoteVolume", 0) >= Config.MIN_24H_VOLUME_USD
            ]
            filtered.sort(key=lambda x: x["volume"], reverse=True)
            return [x["symbol"] for x in filtered[:Config.MAX_COINS_TO_SCAN]]
        except Exception as exc:
            print("[Engine] fetch_tickers failed:", exc)
            return []

    # --- OHLCV -------------------------------------------------------------
    def fetch_df(self, symbol: str, tf: str, limit: int) -> pd.DataFrame:
        ohlcv = self.exchange.fetch_ohlcv(symbol, tf, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["t", "open", "high", "low", "close", "volume"])
        return df.reset_index(drop=True)

    # --- ATR ---------------------------------------------------------------
    def _atr_series(self, df: pd.DataFrame) -> pd.Series:
        prev_close = df["close"].shift().values
        tr = pd.concat(
            [
                df["high"] - df["low"],
                pd.Series(np.abs(df["high"].values - prev_close)),
                pd.Series(np.abs(df["low"].values - prev_close)),
            ],
            axis=1,
        ).max(axis=1)
        return tr.rolling(14).mean()

    # --- Volatility regime -------------------------------------------------
    def analyze_regime(self, df: pd.DataFrame, symbol: str) -> dict:
        close   = df["close"].iloc[-1]
        atr     = self._atr_series(df).iloc[-1]
        atr_pct = (atr / close) * 100
        floor   = Config.ATR_FLOOR_BTC if "BTC" in symbol else Config.ATR_FLOOR_ALT
        active  = bool(atr_pct > floor)

        # simple mean-revert zone detection: close near 20-period BB mid
        bb_mid    = df["close"].rolling(20).mean().iloc[-1]
        bb_std    = df["close"].rolling(20).std().iloc[-1]
        in_zone   = abs(close - bb_mid) < bb_std * 0.5 if bb_std else False

        label = "ACTIVE"
        if active and in_zone:
            label = "ACTIVE (MEAN REVERT ZONE)"
        elif not active:
            label = "LOW VOL"

        return {"ok": active, "atr_pct": float(atr_pct), "atr": float(atr), "label": label}

    # --- HTF trend ---------------------------------------------------------
    def get_trend(self, df_htf: pd.DataFrame) -> str:
        e50  = df_htf["close"].ewm(span=50).mean().iloc[-1]
        e200 = df_htf["close"].ewm(span=200).mean().iloc[-1]
        c    = df_htf["close"].iloc[-1]
        if c > e50 > e200:
            return "BULLISH"
        if c < e50 < e200:
            return "BEARISH"
        return "NEUTRAL"

    # --- Signal detection + enrichment ------------------------------------
    def detect_signal(self, df_ltf: pd.DataFrame, trend: str, regime: dict) -> dict | None:
        e50    = df_ltf["close"].ewm(span=50).mean().iloc[-1]
        c      = df_ltf["close"].iloc[-1]
        o      = df_ltf["open"].iloc[-1]
        dist   = abs(c - e50) / e50 * 100

        if dist > Config.MAX_EMA_DIST_PCT:
            return None

        vol_ma = df_ltf["volume"].rolling(20).mean().iloc[-1]
        if pd.isna(vol_ma) or vol_ma == 0:
            return None

        vol_spike = df_ltf["volume"].iloc[-1] > vol_ma * 1.2

        # Directional conditions
        is_long  = trend == "BULLISH" and c > e50 and c > o
        is_short = trend == "BEARISH" and c < e50 and c < o

        if not vol_spike or (not is_long and not is_short):
            return None

        side    = "LONG" if is_long else "SHORT"
        prime   = dist < 1.0
        pattern = "Breakout" if not prime else "EMA Pullback"
        style   = "MOMENTUM"

        # --- Confidence ---
        w = Config.CONF_WEIGHTS
        score = (
            w["trend_aligned"] * 1 +          # always true if we reached here
            w["vol_spike"]     * int(vol_spike) +
            w["prime_entry"]   * int(prime) +
            w["regime_active"] * int(regime["ok"])
        )
        conf = {"score": score, "max": sum(w.values())}

        # --- Entry, SL, TPs ---
        entry     = float(c)
        atr       = regime["atr"]
        sl_offset = atr * Config.SL_ATR_MULT
        stop_loss = entry - sl_offset if side == "LONG" else entry + sl_offset
        risk_pct  = abs(entry - stop_loss) / entry * 100
        r_unit    = abs(entry - stop_loss)

        take_profits = []
        for r_mult in Config.TP_R_MULTIPLES:
            tp_price = (
                entry + r_unit * r_mult if side == "LONG"
                else entry - r_unit * r_mult
            )
            take_profits.append({"r": r_mult, "price": round(tp_price, 6)})

        return {
            "side":         side,
            "type":         "PRIME" if prime else "BRK",
            "pattern":      pattern,
            "style":        style,
            "confidence":   conf,
            "entry":        round(entry, 6),
            "stop_loss":    round(stop_loss, 6),
            "risk_pct":     round(risk_pct, 2),
            "take_profits": take_profits,
            "atr_pct":      round(regime["atr_pct"], 2),
            "regime":       regime["label"],
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    engine   = GoatXXEngine()
    state    = BotState(Config.STATE_FILE)
    notifier = TelegramNotifier()
    t_start  = time.time()

    symbols = engine.get_top_volume_symbols()
    notifier.send_message(format_start_message(len(symbols)))

    signals_sent = 0
    scanned      = 0

    for sym in symbols:
        if state.is_on_cooldown(sym):
            continue
        try:
            df_ltf  = engine.fetch_df(sym, Config.LTF_TIMEFRAME, Config.OHLCV_LIMIT)
            df_htf  = engine.fetch_df(sym, Config.HTF_TIMEFRAME, Config.OHLCV_LIMIT)
            regime  = engine.analyze_regime(df_ltf, sym)
            scanned += 1

            if not regime["ok"]:
                continue

            trend = engine.get_trend(df_htf)
            sig   = engine.detect_signal(df_ltf, trend, regime)

            if sig:
                sig["symbol"]    = sym
                sig["trend"]     = trend
                sig["move_3d"]   = _3d_move_pct(df_ltf)
                sig["timestamp"] = utc_now().strftime("%H:%M:%S")

                msg = format_signal_message(sig)
                if notifier.send_message(msg):
                    signals_sent += 1
                    state.set_last_signal_time(sym, time.time())

        except Exception as exc:
            print("[Main] Error on", sym, ":", exc)
            continue

    elapsed = time.time() - t_start
    notifier.send_message(format_summary_message(signals_sent, scanned, elapsed))
    state.save()


if __name__ == "__main__":
    main()
