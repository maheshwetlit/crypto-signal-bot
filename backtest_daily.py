#!/usr/bin/env python3
"""
backtest_daily.py — honest portfolio backtest of strategy_daily.py on Hyperliquid daily candles.

Phases (anti-overfitting):
  dev    Uses ONLY candles before the cutoff (default 2025-07-01). Data after the
         cutoff is never loaded into the strategy. Robustness grid runs here.
  final  Runs once on everything; only trades filled on/after the cutoff are judged.

Pass criteria (final period): >= 50 trades, win rate >= 55%, profit factor >= 1.3,
>= 60% of months profitable, net profit > 0 after all costs, and the average trade
beats 95% of random-timing trades (same coins, same trend filter, same exits & costs).

Usage: python backtest_daily.py --phase dev|final [--cutoff 2025-07-01]
"""
import argparse
import json
import os
import time
from contextlib import contextmanager

import numpy as np
import pandas as pd

import strategy_daily as S
from strategy_daily import D

DAY_MS = S.DAY_MS
CACHE = "backtest_cache_daily"
OUT = "backtest_results"


# ───────────────────────── data ─────────────────────────
def load_all(start="2023-01-01"):
    import hl_data as H
    os.makedirs(CACHE, exist_ok=True)
    today = (int(time.time() * 1000) // DAY_MS) * DAY_MS
    f = os.path.join(CACHE, f"daily_{today}.pkl")
    if os.path.exists(f):
        return pd.read_pickle(f)
    uni = H.universe()
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    data = {}
    for coin in uni["coin"]:
        try:
            df = H.candles(coin, "1d", start_ms, today)
        except Exception as e:
            print(f"  skip {coin}: {e}")
            continue
        df = df[df["t"] < today]  # closed days only
        if len(df) >= 30:
            data[coin] = df
        time.sleep(0.25)
    print(f"loaded {len(data)} coins ({int(uni['delisted'].sum())} delisted in universe)")
    pd.to_pickle(data, f)
    return data


def build_panel(data, cutoff_ms=None):
    frames = []
    for coin, df in data.items():
        d = df if cutoff_ms is None else df[df["t"] < cutoff_ms]
        if len(d) < 30:
            continue
        f = S.features(d.reset_index(drop=True))
        f["coin"] = coin
        frames.append(f)
    panel = pd.concat(frames, ignore_index=True)
    days = {t: g.set_index("coin") for t, g in panel.groupby("t")}
    return panel, days


# ───────────────────────── portfolio engine ─────────────────────────
def run_portfolio(days):
    cal = sorted(days)
    eq_cash, slots = D.START_EQUITY, D.SLOTS_START
    positions, orders, trades, curve = {}, [], [], []

    def close(coin, px, reason, k, t):
        nonlocal eq_cash, slots
        pos = positions.pop(coin)
        held = k - pos["fill_k"]
        p = S.close_pnl(pos, px, held)
        eq_cash += p["net_usd"]
        slots = S.next_slots(slots, p["net_usd"] > 0)
        trades.append({"coin": coin, "side": pos["side"], "signal_t": pos["signal_t"], "fill_t": pos["fill_t"],
                       "exit_t": t, "reason": reason, "entry": pos["entry"], "exit": px, "days": held,
                       "notional": pos["notional"], "stop_pct": pos["stop_pct"], "equity_after": eq_cash, **p})

    for k, t in enumerate(cal):
        day = days[t]
        # 1) exits queued at yesterday's close -> today's open
        for coin in [c for c, p in positions.items() if p.get("queued")]:
            if coin in day.index:
                close(coin, float(day.at[coin, "open"]), positions[coin]["queued"], k, t)
        # 2) stops on positions held from earlier days
        for coin in list(positions):
            if coin in day.index:
                px = S.stop_hit(positions[coin], day.loc[coin])
                if px is not None:
                    close(coin, px, "STOP", k, t)
        # 3) yesterday's limit orders
        for o in orders:
            if o["coin"] in positions or o["coin"] not in day.index:
                continue
            bar = day.loc[o["coin"]]
            fill = S.try_fill(o, bar)
            if fill is None:
                continue
            positions[o["coin"]] = {**o, "entry": fill, "fill_t": t, "fill_k": k, "queued": None}
            px = S.stop_hit(positions[o["coin"]], bar, fill_bar=True)
            if px is not None:
                close(o["coin"], px, "STOP", k, t)
        orders = []
        # 4) close-of-day: mark-to-market + exit signals
        unreal = 0.0
        for coin, pos in positions.items():
            if coin not in day.index:
                continue
            row = day.loc[coin]
            c = float(row["close"])
            unreal += pos["notional"] * ((c - pos["entry"]) / pos["entry"] if pos["side"] == "LONG"
                                         else (pos["entry"] - c) / pos["entry"])
            if not pos["queued"]:
                pos["queued"] = S.exit_signal(pos, row, k - pos["fill_k"])
        equity = eq_cash + unreal
        curve.append((t, equity, eq_cash, len(positions), slots))
        # 5) new signals -> limit orders for tomorrow
        uni = S.rank_universe(day.reset_index())
        cand = day.loc[[c for c in uni if c not in positions]]
        cand = cand[cand["long_setup"] | cand["short_setup"]].sort_values("dv30", ascending=False)
        capacity = slots - len(positions)
        used = sum(p["notional"] for p in positions.values())
        for coin, row in cand.iterrows():
            if capacity <= 0:
                break
            o = S.make_order(coin, "LONG" if row["long_setup"] else "SHORT", row)
            if o is None:
                continue
            n = S.size_notional(equity, o["stop_pct"], used)
            if n < 10:
                break
            orders.append({**o, "notional": n, "signal_t": t})
            used += n
            capacity -= 1
    tr = pd.DataFrame(trades)
    cv = pd.DataFrame(curve, columns=["t", "equity", "cash", "open", "slots"])
    return tr, cv


# ───────────────────────── random-timing baseline ─────────────────────────
def sim_single(f, i, side):
    """One trade from signal row i of coin features f, fixed $1000 notional."""
    o = S.make_order("x", side, f.iloc[i])
    if o is None or i + 1 >= len(f):
        return None
    bar = f.iloc[i + 1]
    fill = S.try_fill(o, bar)
    if fill is None:
        return None
    pos = {**o, "entry": fill, "notional": 1000.0}
    px = S.stop_hit(pos, bar, fill_bar=True)
    if px is not None:
        return S.close_pnl(pos, px, 0)
    queued = S.exit_signal(pos, bar, 0)
    for j in range(i + 2, len(f)):
        b = f.iloc[j]
        if queued:
            return S.close_pnl(pos, float(b["open"]), j - i - 1)
        px = S.stop_hit(pos, b)
        if px is not None:
            return S.close_pnl(pos, px, j - i - 1)
        queued = S.exit_signal(pos, b, j - i - 1)
    return None


def random_baseline(panel, trades, lo, hi, iters=30, seed=11):
    rng = np.random.default_rng(seed)
    by_coin = {c: g.reset_index(drop=True) for c, g in panel.groupby("coin")}
    out = []
    for _ in range(iters):
        res = []
        for _, tr in trades.iterrows():
            f = by_coin[tr["coin"]]
            trend = (f["close"] > f["sma"]) & (f["ret20"] > 0) if tr["side"] == "LONG" else \
                    (f["close"] < f["sma"]) & (f["ret20"] < 0)
            idx = np.where(trend & (f["t"] >= lo) & (f["t"] < hi) & (f["bars"] >= D.MIN_HISTORY))[0]
            idx = idx[idx < len(f) - 2]
            for i in rng.permutation(idx)[:20]:
                r = sim_single(f, int(i), tr["side"])
                if r:
                    res.append(r["net_pct"])
                    break
        if res:
            out.append((float(np.mean(res)), float(np.mean(np.array(res) > 0))))
    return out


# ───────────────────────── metrics ─────────────────────────
def trade_stats(t):
    if t is None or len(t) == 0:
        return {"trades": 0}
    w, l = t.loc[t.net_usd > 0, "net_usd"].sum(), -t.loc[t.net_usd <= 0, "net_usd"].sum()
    m = t.groupby(pd.to_datetime(t["exit_t"], unit="ms", utc=True).dt.strftime("%Y-%m"))["net_usd"].sum()
    return {"trades": int(len(t)), "win_rate": round(float((t.net_usd > 0).mean() * 100), 1),
            "avg_trade_pct": round(float(t.net_pct.mean()), 3), "avg_R": round(float(t.r.mean()), 3),
            "profit_factor": round(float(w / l), 2) if l > 0 else None,
            "net_usd": round(float(t.net_usd.sum()), 1),
            "months_profitable_pct": round(float((m > 0).mean() * 100), 1), "months": int(len(m)),
            "avg_days_held": round(float(t.days.mean()), 1)}


def curve_stats(cv):
    if cv is None or len(cv) < 2:
        return {}
    e = cv["equity"]
    yrs = max((cv.t.iloc[-1] - cv.t.iloc[0]) / DAY_MS / 365, 1e-9)
    return {"start_equity": round(float(e.iloc[0]), 1), "end_equity": round(float(e.iloc[-1]), 1),
            "return_pct": round(float((e.iloc[-1] / e.iloc[0] - 1) * 100), 1),
            "cagr_pct": round(float(((e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1) * 100), 1),
            "max_drawdown_pct": round(float((e / e.cummax() - 1).min() * 100), 1),
            "avg_open_positions": round(float(cv["open"].mean()), 2)}


def md(rows, cols):
    s = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    for r in rows:
        s += "| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + " |\n"
    return s


@contextmanager
def override(**kw):
    old = {k: getattr(D, k) for k in kw}
    for k, v in kw.items():
        setattr(D, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(D, k, v)


TCOLS = ["trades", "win_rate", "avg_trade_pct", "avg_R", "profit_factor", "net_usd", "months_profitable_pct", "avg_days_held"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["dev", "final"], required=True)
    ap.add_argument("--cutoff", default="2025-07-01")
    a = ap.parse_args()
    cutoff_ms = int(pd.Timestamp(a.cutoff, tz="UTC").timestamp() * 1000)
    out_dir = os.path.join(OUT, f"daily_{a.phase}")
    os.makedirs(out_dir, exist_ok=True)
    fmt = lambda ms: pd.to_datetime(ms, unit="ms", utc=True).strftime("%Y-%m-%d")

    data = load_all()
    panel, days = build_panel(data, cutoff_ms if a.phase == "dev" else None)
    trades, cv = run_portfolio(days)
    first, last = min(days), max(days)
    lo, hi = (first, cutoff_ms) if a.phase == "dev" else (cutoff_ms, last + DAY_MS)
    judged = trades[(trades.fill_t >= lo) & (trades.fill_t < hi)] if len(trades) else trades
    cvj = cv[(cv.t >= lo) & (cv.t < hi)]

    rep = [f"# Daily strategy backtest — {S.VERSION} — phase: {a.phase.upper()}\n",
           f"Generated {pd.Timestamp.utcnow():%Y-%m-%d %H:%M} UTC · Hyperliquid daily candles · {len(data)} coins "
           f"(top {D.TOP_N_VOLUME} by 30-day volume traded each day) · start ${D.START_EQUITY:,.0f}\n",
           f"Costs: entry maker {D.MAKER_FEE_PCT}%, exit taker {D.TAKER_FEE_PCT}% + {D.SLIPPAGE_PCT}% slippage, "
           f"funding {D.FUNDING_PCT_PER_DAY}%/day always paid\n",
           f"Data: {fmt(first)} → {fmt(last)} · Judged period: {fmt(lo)} → {fmt(hi - DAY_MS)}"
           + (" (data after the cutoff was NOT loaded)" if a.phase == "dev" else " (untouched until now)") + "\n"]

    st = trade_stats(judged)
    cs = curve_stats(cvj)
    rows = [{"set": "ALL", **st}]
    if len(judged):
        for side in ("LONG", "SHORT"):
            rows.append({"set": side, **trade_stats(judged[judged.side == side])})
    rep.append("\n## Trades\n" + md(rows, ["set"] + TCOLS))
    rep.append("\n## Account ($1,000 start, your slot rule, 1% risk, ≤3x)\n" + md([cs], list(cs) or ["start_equity"]))

    verdict = False
    if len(judged):
        judged = judged.copy()
        judged["year"] = pd.to_datetime(judged.exit_t, unit="ms", utc=True).dt.year
        rep.append("\n## By year\n" + md([{"year": y, **trade_stats(g)} for y, g in judged.groupby("year")], ["year"] + TCOLS))
        rep.append("\n## Exit reasons\n" + md([{"reason": r, "n": len(g), "win_rate": round((g.net_usd > 0).mean() * 100, 1),
                                                 "avg_trade_pct": round(g.net_pct.mean(), 3)} for r, g in judged.groupby("reason")],
                                               ["reason", "n", "win_rate", "avg_trade_pct"]))
        base = random_baseline(panel, judged, lo, hi)
        if base:
            bm = np.array([b[0] for b in base]); bw = np.array([b[1] for b in base]) * 100
            beats = st["avg_trade_pct"] > np.percentile(bm, 95)
            rep.append("\n## Versus random timing (same coins, same trend filter, same exits & costs)\n" + md(
                [{"strategy_avg_trade_pct": st["avg_trade_pct"], "random_avg_trade_pct": round(float(bm.mean()), 3),
                  "random_95th_pct": round(float(np.percentile(bm, 95)), 3), "strategy_win_rate": st["win_rate"],
                  "random_win_rate": round(float(bw.mean()), 1), "beats_95pct_of_random": bool(beats)}],
                ["strategy_avg_trade_pct", "random_avg_trade_pct", "random_95th_pct", "strategy_win_rate", "random_win_rate", "beats_95pct_of_random"]))
        else:
            beats = False
        checks = {"trades >= 50": st["trades"] >= 50, "win rate >= 55%": st["win_rate"] >= 55,
                  "profit factor >= 1.3": (st["profit_factor"] or 0) >= 1.3,
                  ">= 60% months profitable": st["months_profitable_pct"] >= 60,
                  "net profit > 0": st["net_usd"] > 0, "beats random timing": bool(beats)}
        verdict = all(checks.values())
        rep.append("\n## Checks\n" + "\n".join(f"- {'✅' if v else '❌'} {k}" for k, v in checks.items()) + "\n")
        trades.to_csv(os.path.join(out_dir, "trades.csv"), index=False)

    if a.phase == "dev" and len(trades):
        grid = [("baseline", {}), ("RSI 15/85", {"RSI_LONG": 15, "RSI_SHORT": 85}), ("RSI 25/75", {"RSI_LONG": 25, "RSI_SHORT": 75}),
                ("stop 2.0 ATR", {"STOP_ATR": 2.0}), ("stop 3.0 ATR", {"STOP_ATR": 3.0}),
                ("limit offset 0", {"LIMIT_OFFSET_ATR": 0.0}), ("limit offset 0.5", {"LIMIT_OFFSET_ATR": 0.5}),
                ("time stop 7d", {"TIME_STOP_DAYS": 7}), ("time stop 14d", {"TIME_STOP_DAYS": 14}),
                ("exit lookback 3", {"EXIT_LOOKBACK": 3}), ("exit lookback 7", {"EXIT_LOOKBACK": 7}),
                ("top 20 coins", {"TOP_N_VOLUME": 20}), ("top 50 coins", {"TOP_N_VOLUME": 50}),
                ("longs only", {"ALLOW_SHORTS": False}), ("funding 0.06%/day", {"FUNDING_PCT_PER_DAY": 0.06})]
        g = []
        for name, kw in grid:
            with override(**kw):
                _, dd = build_panel(data, cutoff_ms)
                t2, c2 = run_portfolio(dd)
            g.append({"variant": name, **trade_stats(t2), "max_dd_pct": curve_stats(c2).get("max_drawdown_pct")})
        rep.append("\n## Robustness (DEV only — a real edge is a plateau, not a single lucky setting)\n" +
                   md(g, ["variant"] + TCOLS + ["max_dd_pct"]))

    rep.append("\n## Verdict\n" + (f"**PASS** ({a.phase})\n" if verdict else f"**FAIL** ({a.phase})\n"))
    open(os.path.join(out_dir, "report.md"), "w").write("\n".join(rep))
    json.dump({"phase": a.phase, "verdict": "PASS" if verdict else "FAIL", "trades": st, "account": cs},
              open(os.path.join(out_dir, "summary.json"), "w"), indent=2)
    print("\n".join(rep))


if __name__ == "__main__":
    main()
