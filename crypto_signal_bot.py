#!/usr/bin/env python3
"""
crypto_signal_bot.py — Hermes v12 daily dip-in-trend, PAPER trading on Hyperliquid data.

Runs every hour (GitHub Actions):
  * hourly:  replays the last closed 1h candles to fill paper limit orders and
             trigger stops (stop checked before anything else in a candle).
  * daily:   after each daily close (00:00 UTC): exit checks (bounce / time stop),
             scan of the most liquid perps, new limit orders, daily summary,
             safety switch.
Rules live in strategy_daily.py (shared with backtest_daily.py).
No account or API key is needed for paper mode.
"""
import json
import os
import time
import traceback
from datetime import datetime, timezone

import requests

import hl_data as H
import strategy_daily as S
from strategy_daily import D

DAY_MS, HOUR_MS = S.DAY_MS, H.HOUR_MS
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "signals_log.json")
STATE_FILE = os.path.join(HERE, "bot_state.json")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_TOP_BY_24H_VOLUME = 60     # pre-filter before ranking by 30-day volume
HALT_DD_PCT = 25.0              # safety switch: stop new entries beyond this drawdown
VENUE = "Hyperliquid (paper)"


def now_ms():
    return int(time.time() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(path + ".tmp", path)


def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        print(f"[TG-OFF] {msg}")
        return
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=15)
            if r.status_code == 200:
                return
            print(f"Telegram {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"Telegram error: {e}")
        time.sleep(2)


def fmt(p):
    return f"{p:.6g}"


def new_state():
    t = now_ms()
    return {"strategy": S.VERSION, "equity_cash": D.START_EQUITY, "slots": D.SLOTS_START,
            "peak_equity": D.START_EQUITY, "halted": False,
            "last_hour_t": (t // HOUR_MS) * HOUR_MS, "last_daily_t": (t // DAY_MS) * DAY_MS - DAY_MS,
            "orders": [], "positions": {}}


# ───────────────────────── trade bookkeeping ─────────────────────────
def open_position(st, log, o, fill, t):
    pos = {**o, "entry": fill, "fill_t": int(t)}
    pos["id"] = f"{datetime.fromtimestamp(t / 1000, tz=timezone.utc):%Y%m%d-%H}-{o['coin']}-{o['side'][0]}"
    st["positions"][o["coin"]] = pos
    log.append({"id": pos["id"], "strategy": S.VERSION, "time": iso(t), "pair": f"{o['coin']}/USD",
                "exchange": VENUE, "direction": o["side"], "style": "DAILY_DIP_IN_TREND",
                "entry": fill, "sl": o["stop"], "tp1": None, "tp_main": None,
                "notional": round(o["notional"], 2), "risk_pct": round(o["stop_pct"], 2),
                "capital": round(o["notional"], 2), "status": "OPEN", "result": None,
                "exit_price": None, "pnl_usd": None, "closed_at": None, "exit_time": None})
    tg(f"⚡ <b>{o['side']} filled</b> <code>{o['coin']}</code> (paper)\n"
       f"Entry: <code>{fmt(fill)}</code> · Size: ${o['notional']:,.0f}\n"
       f"Stop: <code>{fmt(o['stop'])}</code> (−{o['stop_pct']:.1f}%, risk ≈ ${o['notional'] * o['stop_pct'] / 100:,.0f})\n"
       f"Exit on bounce or after {D.TIME_STOP_DAYS} days · Slots {len(st['positions'])}/{st['slots']}")


def close_position(st, log, coin, px, reason, t):
    pos = st["positions"].pop(coin)
    held = int((t // DAY_MS) * DAY_MS - (pos["fill_t"] // DAY_MS) * DAY_MS) // DAY_MS
    p = S.close_pnl(pos, px, held)
    st["equity_cash"] += p["net_usd"]
    won = p["net_usd"] > 0
    st["slots"] = S.next_slots(st["slots"], won)
    for rec in log:
        if rec.get("id") == pos["id"]:
            rec.update({"status": "WIN" if won else "LOSS", "result": "WIN" if won else "LOSS",
                        "exit_reason": reason, "exit_price": px, "closed_at": iso(t), "exit_time": iso(t),
                        "days_held": held, "pnl_usd": round(p["net_usd"], 2), "pnl_pct": round(p["net_pct"], 3),
                        "cost_usd": round(p["cost_usd"], 2), "r_multiple": round(p["r"], 2),
                        "equity_after": round(st["equity_cash"], 2)})
    tg(f"{'✅' if won else '❌'} <b>CLOSED {pos['side']}</b> <code>{coin}</code> (paper)\n"
       f"Reason: {reason} · {held} day(s)\nExit: <code>{fmt(px)}</code>\n"
       f"P&L after costs: <b>{p['net_usd']:+.2f} USD</b> ({p['net_pct']:+.2f}%)\n"
       f"Account: ${st['equity_cash']:,.2f} · Slots now {st['slots']}")


# ───────────────────────── hourly: fills & stops ─────────────────────────
def hourly(st, log, hour_end):
    coins = set(st["positions"]) | {o["coin"] for o in st["orders"]}
    for coin in sorted(coins):
        try:
            bars = H.closed_only(H.candles(coin, "1h", st["last_hour_t"], hour_end), HOUR_MS, hour_end)
        except Exception as e:
            print(f"[1h] {coin}: {e}")
            continue
        bars = bars[bars["t"] >= st["last_hour_t"]]
        for _, bar in bars.iterrows():
            t = int(bar["t"])
            if coin in st["positions"]:
                px = S.stop_hit(st["positions"][coin], bar)
                if px is not None:
                    close_position(st, log, coin, px, "STOP", t + HOUR_MS)
                continue
            for o in [o for o in st["orders"] if o["coin"] == coin]:
                if not (o["valid_from"] <= t < o["expire_t"]):
                    continue
                fill = S.try_fill(o, bar)
                if fill is None:
                    continue
                st["orders"].remove(o)
                open_position(st, log, o, fill, t)
                px = S.stop_hit(st["positions"][coin], bar, fill_bar=True)
                if px is not None:
                    close_position(st, log, coin, px, "STOP", t + HOUR_MS)
                break
    st["last_hour_t"] = hour_end


# ───────────────────────── daily: exits, scan, orders ─────────────────────────
def daily(st, log, day_t, uni):
    expired = [o for o in st["orders"] if o["expire_t"] <= day_t]
    st["orders"] = [o for o in st["orders"] if o["expire_t"] > day_t]
    marks = dict(zip(uni["coin"], uni["mark"]))
    live = uni[~uni["delisted"]].sort_values("day_ntl_vlm", ascending=False)
    coins = list(dict.fromkeys(list(live["coin"].head(SCAN_TOP_BY_24H_VOLUME)) + list(st["positions"])))
    feats = {}
    for coin in coins:
        try:
            d = H.closed_only(H.candles(coin, "1d", day_t - 400 * DAY_MS, day_t), DAY_MS, day_t)
            if len(d) >= 30:
                feats[coin] = S.features(d)
        except Exception as e:
            print(f"[1d] {coin}: {e}")
        time.sleep(0.2)

    # exits (bounce / time stop) at the current price ~ next open
    for coin in list(st["positions"]):
        f = feats.get(coin)
        if f is None or f.empty or int(f["t"].iloc[-1]) != day_t - DAY_MS:
            continue
        pos = st["positions"][coin]
        held = int((f["t"].iloc[-1] - (pos["fill_t"] // DAY_MS) * DAY_MS) // DAY_MS)
        reason = S.exit_signal(pos, f.iloc[-1], held)
        if reason:
            close_position(st, log, coin, float(marks.get(coin) or f["close"].iloc[-1]), reason, day_t)

    # mark-to-market equity + safety switch
    unreal = 0.0
    for coin, pos in st["positions"].items():
        m = marks.get(coin) or pos["entry"]
        unreal += pos["notional"] * ((m - pos["entry"]) / pos["entry"] if pos["side"] == "LONG" else (pos["entry"] - m) / pos["entry"])
    equity = st["equity_cash"] + unreal
    st["peak_equity"] = max(st.get("peak_equity", equity), equity)
    dd = (equity / st["peak_equity"] - 1) * 100
    if dd <= -HALT_DD_PCT and not st["halted"]:
        st["halted"] = True
        tg(f"🛑 <b>Safety switch</b>: drawdown {dd:.1f}% (limit {HALT_DD_PCT}%). "
           f"No new entries until you reset it. Open trades are still managed.")

    # new limit orders for the coming day
    rows = []
    for coin, f in feats.items():
        if f.empty or int(f["t"].iloc[-1]) != day_t - DAY_MS:
            continue
        r = f.iloc[-1].copy()
        r["coin"] = coin
        rows.append(r)
    placed = []
    if rows and not st["halted"]:
        import pandas as pd
        day = pd.DataFrame(rows)
        uni_set = S.rank_universe(day)
        cand = day[day["coin"].isin(uni_set) & ~day["coin"].isin(list(st["positions"])) &
                   (day["long_setup"] | day["short_setup"])].sort_values("dv30", ascending=False)
        capacity = st["slots"] - len(st["positions"])
        used = sum(p["notional"] for p in st["positions"].values())
        for _, r in cand.iterrows():
            if capacity <= 0:
                break
            o = S.make_order(r["coin"], "LONG" if r["long_setup"] else "SHORT", r)
            if o is None:
                continue
            n = S.size_notional(equity, o["stop_pct"], used)
            if n < 10:
                break
            o.update({"notional": n, "signal_t": int(r["t"]), "valid_from": day_t, "expire_t": day_t + DAY_MS})
            st["orders"].append(o)
            placed.append(o)
            used += n
            capacity -= 1

    lines = [f"📊 <b>Daily summary</b> ({iso(day_t)[:10]}, paper · {S.VERSION})",
             f"Account: <b>${equity:,.2f}</b> (cash ${st['equity_cash']:,.2f}) · drawdown {dd:.1f}%",
             f"Slots: {len(st['positions'])}/{st['slots']}" + (" · 🛑 HALTED" if st["halted"] else "")]
    for coin, p in st["positions"].items():
        m = marks.get(coin) or p["entry"]
        ch = ((m - p["entry"]) / p["entry"] if p["side"] == "LONG" else (p["entry"] - m) / p["entry"]) * 100
        lines.append(f"• {p['side']} {coin} {ch:+.1f}%")
    lines.append(f"New limit orders: {len(placed)}" + (" — " + ", ".join(f"{o['side'][0]} {o['coin']} @ {fmt(o['limit'])}" for o in placed) if placed else ""))
    if expired:
        lines.append(f"Expired unfilled: {', '.join(o['coin'] for o in expired)}")
    top = live.head(5)
    lines.append("Top 24h volume: " + ", ".join(f"{c} ${v / 1e6:,.0f}M" for c, v in zip(top["coin"], top["day_ntl_vlm"])))
    tg("\n".join(lines))
    st["last_daily_t"] = day_t


def main():
    st = load(STATE_FILE, {})
    if st.get("strategy") != S.VERSION:
        st = new_state()
    log = load(LOG_FILE, [])
    t = now_ms()
    hour_end = (t // HOUR_MS) * HOUR_MS
    day_t = (t // DAY_MS) * DAY_MS
    try:
        if hour_end > st["last_hour_t"]:
            hourly(st, log, hour_end)
        if day_t > st["last_daily_t"]:
            daily(st, log, day_t, H.universe())
    except Exception:
        traceback.print_exc()
        tg("⚠️ Hermes v12 run error — check the Actions log")
    save(LOG_FILE, log)
    save(STATE_FILE, st)
    print(f"[RUN] equity_cash={st['equity_cash']:.2f} positions={len(st['positions'])} orders={len(st['orders'])} slots={st['slots']}")


if __name__ == "__main__":
    main()
