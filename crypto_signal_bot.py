#!/usr/bin/env python3
"""
crypto_signal_bot.py — Hermes v11 (trend pullback), PAPER signals on KuCoin.

What changed vs v10 (see STRATEGY.md for the full reasoning):
  * Strategy logic lives in strategy.py and is shared with backtest.py,
    so live == backtest.
  * Signals use CLOSED 15m candles only (v10 read the still-forming candle).
  * Trade exits are replayed from real candles since entry (stop checked
    before target), not from 10-minute ticker snapshots.
  * P&L includes fees + slippage. No averaging down ("grinding") — removed.
  * One writer for signals_log.json (the old validator also closed trades
    with different rules, which caused conflicts and lost trades).
  * Unique trade IDs; trades are never dropped from the log.
"""
import json
import os
import time
import traceback
from datetime import datetime, timezone

import ccxt
import requests

import strategy as S
from strategy import P


class Config:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    EXCHANGE = "KuCoin"
    CAPITAL_PER_SIGNAL = 1000.0
    MIN_24H_VOLUME_USD = 5_000_000
    MAX_COINS_TO_SCAN = 60
    MAX_OPEN_TOTAL = 10
    MAX_OPEN_PER_PAIR = 1
    SIGNAL_LOG_FILE = os.path.join(SCRIPT_DIR, "signals_log.json")
    STATE_FILE = os.path.join(SCRIPT_DIR, "bot_state.json")
    EXCLUDE_BASES = {"USDC", "TUSD", "DAI", "FDUSD", "USDD", "PYUSD", "USDE", "USD1", "EUR", "PAXG", "XAUT", "BUSD"}
    BLOCKLIST = {"H/USDT"}


def now_ms():
    return int(time.time() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def is_tradeable(sym):
    if not sym.endswith("/USDT") or sym in Config.BLOCKLIST:
        return False
    base = sym.split("/")[0]
    if base in Config.EXCLUDE_BASES or base.endswith(("3L", "3S", "2L", "2S", "UP", "DOWN")):
        return False
    return True


# ───────────────────────── storage ─────────────────────────
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


# ───────────────────────── telegram ─────────────────────────
class Telegram:
    def send(self, msg):
        if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
            print(f"[TG-OFF] {msg}")
            return
        for _ in range(3):
            try:
                r = requests.post(f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                  json={"chat_id": Config.TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                                  timeout=15)
                if r.status_code == 200:
                    return
                print(f"Telegram {r.status_code}: {r.text[:200]}")
            except Exception as e:
                print(f"Telegram error: {e}")
            time.sleep(2)


# ───────────────────────── market data ─────────────────────────
def fetch_closed(ex, sym, tf, tf_ms, limit=500, since=None):
    rows = ex.fetch_ohlcv(sym, tf, since=since, limit=limit)
    return S.closed_only(S.to_df(rows), tf_ms, now_ms())


def fmt(p):
    return f"{p:.8g}"


# ───────────────────────── exits ─────────────────────────
def update_open_trades(ex, log, tg):
    """Replay each open trade over the closed 15m candles since its entry bar."""
    closed_n = 0
    for s in log:
        if s.get("status") != "OPEN" or s.get("strategy") != S.VERSION:
            continue
        try:
            bars = fetch_closed(ex, s["pair"], P.TF, P.TF_MS, limit=P.TIME_STOP_BARS + 10, since=s["entry_bar_t"])
            bars = bars[bars["t"] >= s["entry_bar_t"]].reset_index(drop=True)
            trade = {"side": s["direction"], "entry": s["entry"], "sl": s["sl"], "tp": s["tp_main"], "risk": s["risk"]}
            res = S.simulate_exit(trade, bars)
        except Exception as e:
            print(f"[EXIT] {s['pair']}: {e}")
            continue
        if res is None:
            continue
        pnl = Config.CAPITAL_PER_SIGNAL * res["net_pct"] / 100
        exit_bar_t = int(bars["t"].iat[res["bars"] - 1])
        s.update({
            "status": "WIN" if pnl > 0 else "LOSS",
            "result": "WIN" if pnl > 0 else "LOSS",
            "exit_reason": res["reason"],
            "exit_price": res["exit"],
            "closed_at": iso(exit_bar_t + P.TF_MS),
            "exit_time": iso(exit_bar_t + P.TF_MS),
            "bars_held": res["bars"],
            "pnl_gross_pct": round(res["gross_pct"], 3),
            "pnl_pct": round(res["net_pct"], 3),
            "pnl_usd": round(pnl, 2),
            "cost_usd": round(Config.CAPITAL_PER_SIGNAL * P.round_trip_cost(), 2),
            "r_multiple": round(res["r_net"], 2),
        })
        closed_n += 1
        icon = "✅" if pnl > 0 else "❌"
        tg.send(f"{icon} <b>CLOSED {s['direction']}</b> <code>{s['pair']}</code>\n"
                f"Reason: {res['reason']} · {res['bars'] * 15} min\n"
                f"Exit: <code>{fmt(res['exit'])}</code>\n"
                f"P&L after costs: <b>{pnl:+.2f} USD</b> ({res['r_net']:+.2f}R)")
    return closed_n


# ───────────────────────── entries ─────────────────────────
def scan(ex, log, state, tg):
    tickers = ex.fetch_tickers()
    syms = sorted([s for s, t in tickers.items()
                   if is_tradeable(s) and (t.get("quoteVolume") or 0) >= Config.MIN_24H_VOLUME_USD],
                  key=lambda s: -(tickers[s].get("quoteVolume") or 0))[:Config.MAX_COINS_TO_SCAN]
    open_trades = [s for s in log if s.get("status") == "OPEN"]
    if len(open_trades) >= Config.MAX_OPEN_TOTAL:
        print("[SCAN] max open reached")
        return 0
    btc1h = fetch_closed(ex, "BTC/USDT", P.HTF, P.HTF_MS, limit=500)
    sent, cooldowns = 0, state.setdefault("cooldowns_ms", {})
    for sym in syms:
        if len(open_trades) >= Config.MAX_OPEN_TOTAL:
            break
        if sum(1 for s in open_trades if s["pair"] == sym) >= Config.MAX_OPEN_PER_PAIR:
            continue
        if now_ms() < cooldowns.get(sym, 0):
            continue
        try:
            d15 = fetch_closed(ex, sym, P.TF, P.TF_MS, limit=500)
            d1h = btc1h if sym == "BTC/USDT" else fetch_closed(ex, sym, P.HTF, P.HTF_MS, limit=500)
            if len(d15) < 250 or len(d1h) < 250:
                continue
            sig = S.compute_signals(d15, d1h, None if sym == "BTC/USDT" else btc1h)
            last = sig.iloc[-1]
            # act only on the candle that JUST closed (GitHub cron can lag a few minutes)
            if now_ms() - int(last["close_time"]) > P.TF_MS:
                continue
            if not (last["long_sig"] or last["short_sig"]):
                continue
            side = "LONG" if last["long_sig"] else "SHORT"
            px = float(tickers[sym]["last"] or ex.fetch_ticker(sym)["last"])
            tr = S.build_trade(side, px, float(last["sl_long" if side == "LONG" else "sl_short"]))
            if tr is None:
                continue
            # anti-chase: skip if price already ran > MAX_CHASE_R beyond the signal close
            ran = (px - last["close"]) if side == "LONG" else (last["close"] - px)
            if ran > P.MAX_CHASE_R * tr["risk"]:
                print(f"[SKIP] {sym} chased {ran / tr['risk']:.2f}R")
                continue
        except Exception as e:
            print(f"[SCAN] {sym}: {e}")
            continue

        entry_bar_t = (now_ms() // P.TF_MS) * P.TF_MS
        rec = {
            "id": f"{datetime.now(timezone.utc):%Y%m%d-%H%M}-{sym.split('/')[0]}-{side[0]}",
            "strategy": S.VERSION,
            "time": datetime.now(timezone.utc).isoformat(),
            "signal_bar_close": iso(int(last["close_time"])),
            "entry_bar_t": int(entry_bar_t),
            "pair": sym, "exchange": Config.EXCHANGE, "direction": side,
            "style": "TREND_PULLBACK", "tag": f"pullback_{side.lower()}",
            "entry": tr["entry"], "sl": tr["sl"], "tp1": tr["tp"], "tp_main": tr["tp"], "risk": tr["risk"],
            "risk_pct": round(tr["risk"] / tr["entry"] * 100, 3),
            "adx": round(float(last["adx"]), 1), "rsi": round(float(last["rsi"]), 1),
            "volume_x": round(float(last["volr"]), 2), "ext_atr": round(float(last["ext_atr"]), 2),
            "htf_trend": "BULLISH" if side == "LONG" else "BEARISH",
            "capital": Config.CAPITAL_PER_SIGNAL,
            "status": "OPEN", "result": None, "exit_price": None, "pnl_usd": None,
            "closed_at": None, "exit_time": None,
        }
        log.append(rec)
        open_trades.append(rec)
        cooldowns[sym] = now_ms() + (P.TIME_STOP_BARS + P.COOLDOWN_BARS) * P.TF_MS
        sent += 1
        tg.send(f"⚡ <b>{side}</b> <code>{sym}</code> (paper · {S.VERSION})\n"
                f"Entry: <code>{fmt(tr['entry'])}</code>\n"
                f"Stop: <code>{fmt(tr['sl'])}</code> (−{rec['risk_pct']}%)\n"
                f"Target: <code>{fmt(tr['tp'])}</code> ({P.TP_R}R) · time stop {P.TIME_STOP_BARS * 15 // 60}h\n"
                f"ADX {rec['adx']} · RSI {rec['rsi']} · Vol {rec['volume_x']}x")
    return sent


def main():
    ex = ccxt.kucoin({"enableRateLimit": True})
    tg = Telegram()
    log = load_json(Config.SIGNAL_LOG_FILE, [])
    state = load_json(Config.STATE_FILE, {})
    state.pop("cooldowns", None)  # v10 format
    try:
        closed = update_open_trades(ex, log, tg)
        save_json(Config.SIGNAL_LOG_FILE, log)
        sent = scan(ex, log, state, tg)
    except Exception:
        traceback.print_exc()
        tg.send("⚠️ Hermes v11 run error — check Actions log")
        closed, sent = 0, 0
    # prune expired cooldowns
    state["cooldowns_ms"] = {k: v for k, v in state.get("cooldowns_ms", {}).items() if v > now_ms()}
    save_json(Config.SIGNAL_LOG_FILE, log)
    save_json(Config.STATE_FILE, state)
    print(f"[RUN] closed={closed} new={sent} open={sum(1 for s in log if s.get('status') == 'OPEN')}")


if __name__ == "__main__":
    main()
