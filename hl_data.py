"""
hl_data.py — Hyperliquid public market data (no account, no API key needed).

Exchange adapter: everything venue-specific lives here, so switching to
another venue (e.g. Lighter) means writing one more file like this.
"""
import time

import pandas as pd
import requests

API = "https://api.hyperliquid.xyz/info"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
_session = requests.Session()


def _post(body, retries=6):
    for attempt in range(retries):
        try:
            r = _session.post(API, json=body, timeout=30)
            if r.status_code == 429:
                time.sleep(5 + 5 * attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  [hl] retry {body.get('type')}: {e}")
            time.sleep(2 + 2 * attempt)


def universe():
    """All perp markets with live context. Returns DataFrame:
    coin, delisted, day_ntl_vlm, funding (hourly rate), mark, prev_day_px, max_lev"""
    meta, ctxs = _post({"type": "metaAndAssetCtxs"})
    rows = []
    for u, c in zip(meta["universe"], ctxs):
        rows.append({
            "coin": u["name"],
            "delisted": bool(u.get("isDelisted", False)),
            "max_lev": u.get("maxLeverage"),
            "day_ntl_vlm": float(c.get("dayNtlVlm") or 0),
            "funding": float(c.get("funding") or 0),
            "mark": float(c.get("markPx") or 0) if c.get("markPx") else None,
            "prev_day_px": float(c.get("prevDayPx") or 0) if c.get("prevDayPx") else None,
        })
    return pd.DataFrame(rows)


def candles(coin, interval, start_ms, end_ms):
    """OHLCV candles (t = open time, ms). Pages forward; HL returns <= 5000 per call."""
    out, since = [], int(start_ms)
    step = {"1h": HOUR_MS, "1d": DAY_MS}[interval]
    while since < end_ms:
        rows = _post({"type": "candleSnapshot",
                      "req": {"coin": coin, "interval": interval, "startTime": since, "endTime": int(end_ms)}})
        if not rows:
            break
        out.extend(rows)
        nxt = int(rows[-1]["t"]) + step
        if nxt <= since or len(rows) < 2:
            break
        since = nxt
    if not out:
        return pd.DataFrame(columns=["t", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame({"t": [int(r["t"]) for r in out],
                       "open": [float(r["o"]) for r in out], "high": [float(r["h"]) for r in out],
                       "low": [float(r["l"]) for r in out], "close": [float(r["c"]) for r in out],
                       "volume": [float(r["v"]) for r in out]})
    return df.drop_duplicates("t").sort_values("t").reset_index(drop=True)


def closed_only(df, interval_ms, now_ms):
    return df[df["t"] + interval_ms <= now_ms].reset_index(drop=True)
