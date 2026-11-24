#!/usr/bin/env python3
"""
backtest.py — honest backtest for strategy.py (the exact code the live bot runs).

Anti-overfitting design:
  * Data is split by TIME: first 60% = in-sample (develop), last 40% =
    out-of-sample (judge). Only the out-of-sample result decides go/no-go.
  * Every trade pays 0.10% fee + 0.05% slippage per side.
  * Stop-before-target when a candle touches both.
  * Random-entry baseline: same pairs, same period, same direction mix,
    same exits. The strategy must clearly beat random entries.
  * Robustness: neighbouring parameter values are reported (in-sample) —
    a real edge is a plateau, not a single lucky number.
  * Survivorship note: pairs are today's most liquid KuCoin pairs.

Usage: python backtest.py --days 180 --pairs 40
Writes backtest_results/report.md and backtest_results/trades.csv
"""
import argparse
import json
import os
import time
from contextlib import contextmanager

import numpy as np
import pandas as pd

import strategy as S
from strategy import P

OUT_DIR = "backtest_results"
EXCLUDE_BASES = {"USDC", "TUSD", "DAI", "FDUSD", "USDD", "PYUSD", "USDE", "USD1", "EUR", "PAXG", "XAUT", "BUSD"}


def is_tradeable(sym):
    if not sym.endswith("/USDT"):
        return False
    base = sym.split("/")[0]
    if base in EXCLUDE_BASES:
        return False
    if base.endswith(("3L", "3S", "2L", "2S", "UP", "DOWN")):
        return False
    return True


# ───────────────────────── data ─────────────────────────
def fetch_history(ex, sym, tf, tf_ms, start_ms, end_ms):
    out, since = [], start_ms
    while since < end_ms:
        for attempt in range(4):
            try:
                rows = ex.fetch_ohlcv(sym, tf, since=since, limit=1500)
                break
            except Exception as e:
                print(f"  retry {sym} {tf}: {e}")
                time.sleep(2 + 3 * attempt)
        else:
            rows = []
        if not rows:
            break
        out.extend(rows)
        nxt = rows[-1][0] + tf_ms
        if nxt <= since:
            break
        since = nxt
    df = S.to_df(out) if out else S.to_df([])
    return df[df["t"] < end_ms].reset_index(drop=True)


def load_data(days, n_pairs, end_days_ago=0, cache="backtest_cache"):
    import ccxt
    ex = ccxt.kucoin({"enableRateLimit": True})
    os.makedirs(cache, exist_ok=True)
    end_ms = (int(time.time() * 1000) // P.HTF_MS) * P.HTF_MS - end_days_ago * 86_400_000
    start_ms = end_ms - days * 86_400_000
    warm_ms = 250 * P.HTF_MS  # EMA200 warm-up on 1h
    tick = ex.fetch_tickers()
    pairs = sorted([s for s, t in tick.items() if is_tradeable(s) and (t.get("quoteVolume") or 0) >= 5_000_000],
                   key=lambda s: -(tick[s].get("quoteVolume") or 0))[:n_pairs]
    if "BTC/USDT" not in pairs:
        pairs = ["BTC/USDT"] + pairs[:-1]
    data = {}
    for sym in pairs:
        f = os.path.join(cache, sym.replace("/", "_") + f"_{P.TF}_{P.HTF}_{days}_{end_days_ago}.pkl")
        if os.path.exists(f):
            data[sym] = pd.read_pickle(f)
            continue
        print(f"fetch {sym}")
        d15 = fetch_history(ex, sym, P.TF, P.TF_MS, start_ms - warm_ms, end_ms)
        d1h = fetch_history(ex, sym, P.HTF, P.HTF_MS, start_ms - warm_ms, end_ms)
        if len(d15) < 500 or len(d1h) < 250:
            print(f"  skip {sym}: not enough history")
            continue
        data[sym] = (d15, d1h)
        pd.to_pickle(data[sym], f)
    return data, start_ms, end_ms


# ───────────────────────── engine ─────────────────────────
def run_pair(sym, d15, d1h, btc1h, start_ms):
    sig = S.compute_signals(d15, d1h, None if sym == "BTC/USDT" else btc1h)
    trades, i, n = [], 0, len(sig)
    ls, ss = sig["long_sig"].to_numpy(), sig["short_sig"].to_numpy()
    ct = sig["close_time"].to_numpy()
    while i < n - 1:
        if ct[i] < start_ms or not (ls[i] or ss[i]):
            i += 1
            continue
        side = "LONG" if ls[i] else "SHORT"
        entry = sig["open"].iat[i + 1]
        tr = S.build_trade(side, entry, sig["sl_long" if side == "LONG" else "sl_short"].iat[i])
        if tr is None:
            i += 1
            continue
        res = S.simulate_exit(tr, sig.iloc[i + 1:i + 1 + P.TIME_STOP_BARS + 1])
        if res is None:
            break
        trades.append({"pair": sym, "side": side, "signal_time": int(ct[i]),
                       "entry_time": int(sig["t"].iat[i + 1]), **tr, **res,
                       "adx": sig["adx"].iat[i], "volr": sig["volr"].iat[i], "rsi": sig["rsi"].iat[i]})
        i = i + 1 + res["bars"] + P.COOLDOWN_BARS
    return trades, sig


def run_all(data, start_ms):
    btc1h = data["BTC/USDT"][1] if "BTC/USDT" in data else None
    trades, sigs = [], {}
    for sym, (d15, d1h) in data.items():
        t, s = run_pair(sym, d15, d1h, btc1h, start_ms)
        trades += t
        sigs[sym] = s
    df = pd.DataFrame(trades)
    if not df.empty:
        df = df.sort_values("entry_time").reset_index(drop=True)
    return df, sigs


def random_baseline(trades, sigs, split_ms, end_ms, start_ms, iters=30, seed=7):
    """Random entries with the same count/direction per pair and period, same exit rules."""
    rng = np.random.default_rng(seed)
    results = {"IS": [], "OOS": []}
    for _ in range(iters):
        acc = {"IS": [], "OOS": []}
        for (pair, side, period), grp in trades.groupby(["pair", "side", "period"]):
            s = sigs[pair]
            lo, hi = (start_ms, split_ms) if period == "IS" else (split_ms, end_ms)
            idx = np.where((s["close_time"] >= lo) & (s["close_time"] < hi))[0]
            idx = idx[(idx > 200) & (idx < len(s) - P.TIME_STOP_BARS - 2)]
            if len(idx) == 0:
                continue
            for i in rng.choice(idx, size=len(grp), replace=True):
                tr = S.build_trade(side, s["open"].iat[i + 1], s["sl_long" if side == "LONG" else "sl_short"].iat[i])
                if tr is None:
                    continue
                r = S.simulate_exit(tr, s.iloc[i + 1:i + 2 + P.TIME_STOP_BARS])
                if r:
                    acc[period].append(r["r_net"])
        for k in acc:
            if acc[k]:
                results[k].append(float(np.mean(acc[k])))
    return results


# ───────────────────────── metrics ─────────────────────────
def stats(t, capital=1000.0):
    if t is None or len(t) == 0:
        return {"trades": 0}
    pnl = t["net_pct"] / 100 * capital
    wins = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    eq = pnl.cumsum()
    days = max(1.0, (t["entry_time"].max() - t["entry_time"].min()) / 86_400_000)
    return {"trades": int(len(t)), "trades_per_day": round(float(len(t) / days), 2),
            "win_rate": round(float((pnl > 0).mean()) * 100, 1),
            "avg_R": round(float(t["r_net"].mean()), 3),
            "net_usd": round(float(pnl.sum()), 1),
            "per_trade_usd": round(float(pnl.mean()), 2),
            "profit_factor": round(float(wins / losses), 2) if losses > 0 else None,
            "max_dd_usd": round(float((eq - eq.cummax()).min()), 1)}


@contextmanager
def override(**kw):
    old = {k: getattr(P, k) for k in kw}
    for k, v in kw.items():
        setattr(P, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(P, k, v)


def md_table(rows, cols):
    s = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    for r in rows:
        s += "| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + " |\n"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--pairs", type=int, default=40)
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--htf", default="1h")
    ap.add_argument("--end-days-ago", type=int, default=0)
    ap.add_argument("--max-risk", type=float, default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--no-robustness", action="store_true")
    a = ap.parse_args()
    P.use_timeframes(a.tf, a.htf)
    if a.max_risk:
        P.MAX_RISK_PCT = a.max_risk
    global OUT_DIR
    if a.tag:
        OUT_DIR = os.path.join(OUT_DIR, a.tag)

    data, start_ms, end_ms = load_data(a.days, a.pairs, a.end_days_ago)
    split_ms = start_ms + int(0.6 * (end_ms - start_ms))
    trades, sigs = run_all(data, start_ms)
    os.makedirs(OUT_DIR, exist_ok=True)
    cols = ["trades", "trades_per_day", "win_rate", "avg_R", "per_trade_usd", "net_usd", "profit_factor", "max_dd_usd"]
    fmt = lambda ms: pd.to_datetime(ms, unit="ms", utc=True).strftime("%Y-%m-%d")

    rep = [f"# Backtest report — {S.VERSION}\n",
           f"Generated {pd.Timestamp.utcnow():%Y-%m-%d %H:%M} UTC · {len(data)} KuCoin pairs · {P.TF} candles, {P.HTF} trend · max risk {P.MAX_RISK_PCT}% · "
           f"$1,000 per trade · costs {P.FEE_PCT}% fee + {P.SLIPPAGE_PCT}% slippage per side\n",
           f"In-sample: {fmt(start_ms)} → {fmt(split_ms)} · Out-of-sample: {fmt(split_ms)} → {fmt(end_ms)}\n"]

    if trades.empty:
        rep.append("\n**No trades generated.**\n")
    else:
        trades["period"] = np.where(trades["entry_time"] < split_ms, "IS", "OOS")
        trades.to_csv(os.path.join(OUT_DIR, "trades.csv"), index=False)
        rows = []
        for name, sub in [("In-sample", trades[trades.period == "IS"]), ("OUT-OF-SAMPLE", trades[trades.period == "OOS"]),
                          ("OOS longs", trades[(trades.period == "OOS") & (trades.side == "LONG")]),
                          ("OOS shorts", trades[(trades.period == "OOS") & (trades.side == "SHORT")])]:
            rows.append({"set": name, **stats(sub)})
        rep.append("\n## Results\n" + md_table(rows, ["set"] + cols))

        base = random_baseline(trades, sigs, split_ms, end_ms, start_ms)
        brow = []
        for k, lab in (("IS", "In-sample"), ("OOS", "OUT-OF-SAMPLE")):
            real = trades[trades.period == k]["r_net"].mean() if (trades.period == k).any() else float("nan")
            b = np.array(base[k]) if base[k] else np.array([np.nan])
            brow.append({"set": lab, "strategy_avg_R": round(float(real), 3),
                         "random_avg_R": round(float(np.nanmean(b)), 3),
                         "random_95th_pct_R": round(float(np.nanpercentile(b, 95)), 3),
                         "beats_95pct_of_random": bool(real > np.nanpercentile(b, 95))})
        rep.append("\n## Versus random entries (same exits & costs)\n" +
                   md_table(brow, ["set", "strategy_avg_R", "random_avg_R", "random_95th_pct_R", "beats_95pct_of_random"]))

        trades["month"] = pd.to_datetime(trades["entry_time"], unit="ms", utc=True).dt.strftime("%Y-%m")
        mrows = [{"month": m, **stats(g)} for m, g in trades.groupby("month")]
        rep.append("\n## By month\n" + md_table(mrows, ["month"] + cols))
        rep.append("\n## Exit reasons\n" + md_table(
            [{"reason": r, "n": len(g), "avg_R": round(g["r_net"].mean(), 3)} for r, g in trades.groupby("reason")],
            ["reason", "n", "avg_R"]))
        prow = trades.groupby("pair")["net_pct"].agg(["size", "sum"]).sort_values("sum")
        rep.append("\n## Pairs (net % summed)\nWorst: " + ", ".join(f"{p} {v:.1f}%" for p, v in prow["sum"].head(5).items()) +
                   "\n\nBest: " + ", ".join(f"{p} {v:.1f}%" for p, v in prow["sum"].tail(5).items()) + "\n")

        oos = stats(trades[trades.period == "OOS"])
        verdict = (oos.get("trades", 0) >= 100 and (oos.get("avg_R") or 0) > 0.05 and
                   (oos.get("profit_factor") or 0) >= 1.15 and brow[1]["beats_95pct_of_random"])
        rep.append("\n## Verdict\n" + ("**PASS** — out-of-sample is positive after costs and beats random entries. "
                                       "Next step: forward paper-trade 4–8 weeks before any real money.\n" if verdict else
                                       "**FAIL** — out-of-sample does not show a robust edge after costs. "
                                       "Do NOT trade this with real money. Do not tune parameters on the OOS period.\n"))
        json.dump({"verdict": "PASS" if verdict else "FAIL", "oos": oos, "is": rows[0]},
                  open(os.path.join(OUT_DIR, "summary.json"), "w"), indent=2)

    if not a.no_robustness and not trades.empty:
        grid = [("baseline", {}),
                ("TP 1.5R", {"TP_R": 1.5}), ("TP 2.5R", {"TP_R": 2.5}),
                ("ADX max 35", {"ADX_RANGE": (18, 35)}), ("ADX max 50", {"ADX_RANGE": (18, 50)}),
                ("ext max 0.75 ATR", {"MAX_EXT_ATR": 0.75}), ("ext max 1.5 ATR", {"MAX_EXT_ATR": 1.5}),
                ("time stop x0.67", {"TIME_STOP_BARS": 16}), ("time stop x1.33", {"TIME_STOP_BARS": 32}),
                ("no BTC filter", {"USE_BTC_FILTER": False})]
        g_rows = []
        for name, kw in grid:
            with override(**kw):
                t, _ = run_all(data, start_ms)
            t = t[t["entry_time"] < split_ms] if not t.empty else t
            g_rows.append({"variant": name, **stats(t)})
        rep.append("\n## Robustness (IN-SAMPLE only — look for a plateau, not a peak)\n" +
                   md_table(g_rows, ["variant"] + cols))

    open(os.path.join(OUT_DIR, "report.md"), "w").write("\n".join(rep))
    print("\n".join(rep))


if __name__ == "__main__":
    main()
