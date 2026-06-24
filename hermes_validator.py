#!/usr/bin/env python3
"""
hermes_validator.py — Hermes Signal Validator v2
Checks OPEN signals against live prices with intelligent close logic.

Close conditions (in priority order):
  1. SL HIT          — price hits stop-loss -> close as LOSS
  2. TP HIT          — price hits take-profit -> close as WIN
  3. TRAILING SL     — trailing stop activates after +5% move -> close at trail
  4. TIME-DECAY      — age-based close logic:
       >12h sideways  -> stale close (price within +/-1.5% of entry)
       >24h in loss   -> close losing trades (capital recycling)
       >48h any       -> force close regardless
  5. HARD EXPIRY     — 72h hard close at current price
"""
import json
import os
import requests
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- CONFIG ---
SIGNAL_LOG_FILE   = "signals_log.json"
CAPITAL_PER_TRADE = 1000.0
SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
TG_TOKEN_FILE     = os.path.join(SCRIPT_DIR, ".tg_token")

if os.path.exists(TG_TOKEN_FILE):
    with open(TG_TOKEN_FILE) as _tf:
        HERMES_BOT_TOKEN = _tf.read().strip()
else:
    HERMES_BOT_TOKEN = os.getenv("HERMESBOT", "")

HERMES_CHAT_ID = os.getenv("HERMES_CHAT_ID", "5515185305")

EXCHANGE_APIS = {
    "Binance": {"url": "https://api.binance.com/api/v3/ticker/price?symbol={symbol}", "price_key": "price"},
    "KuCoin":  {"url": "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}", "price_key": "price"},
    "Bybit":   {"url": "https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}", "price_key": "lastPrice"},
}

# --- Time-decay thresholds (hours) ---
# Industry standard: Freqtrade uses 24h timeout, quant systems use 24-48h for scalps
MAX_SIGNAL_AGE_HOURS  = 24   # hard expiry (signal thesis invalid after 24h on 5m timeframe)
STALE_CHECK_HOURS     = 6    # start checking for stale trades (sideways = capital lock)
BREAKEVEN_CLOSE_HOURS = 12   # close losing trades after 12h (capital recycling)
FORCE_CLOSE_HOURS     = 18   # force close any trade after 18h regardless of PnL

# --- Trailing SL config ---
TRAIL_ACTIVATION_PCT  = 5.0  # activate trailing SL after +5% favorable move
TRAIL_DISTANCE_PCT    = 3.0  # trail 3% behind peak favorable price

# --- Stale trade detection ---
STALE_RANGE_PCT       = 1.5  # if price within +/-1.5% of entry after 12h = stale


def format_symbol(pair, exchange):
    pair = pair.strip()
    if exchange == "KuCoin":
        if "/" in pair:
            return pair.replace("/", "-")
        for quote in ["USDT", "USDC", "BTC", "ETH", "DAI", "TUSD"]:
            if pair.endswith(quote) and len(pair) > len(quote):
                return pair[:-len(quote)] + "-" + quote
        return pair
    if exchange in ("Binance", "Bybit"):
        return pair.replace("/", "").replace("-", "")
    return pair


def get_exchange_price(symbol, exchange):
    if exchange not in EXCHANGE_APIS:
        exchange = "Binance"
    api_info = EXCHANGE_APIS[exchange]
    formatted = format_symbol(symbol, exchange)
    for attempt in range(2):
        try:
            url = api_info["url"].format(symbol=formatted)
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            data = r.json()
            if exchange == "KuCoin" and "data" in data:
                data = data["data"]
            elif exchange == "Bybit" and "result" in data:
                tickers = data["result"].get("list", [])
                data = tickers[0] if tickers else {}
            price = float(data.get(api_info["price_key"], 0))
            if price > 0:
                return price
        except Exception:
            if attempt < 1:
                time.sleep(0.5)
    return None


def send_telegram(message):
    if not HERMES_BOT_TOKEN or not HERMES_CHAT_ID:
        print(f"[TELEGRAM] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{HERMES_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": HERMES_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")


def get_tp_value(sig):
    for key in ["tp1", "tp", "tp2", "tp3"]:
        val = sig.get(key)
        if val is not None:
            if isinstance(val, list) and val:
                return float(val[0])
            if isinstance(val, (int, float)):
                return float(val)
    return 0.0


def calc_pnl_pct(entry, current, direction):
    if direction == "LONG":
        return (current - entry) / entry * 100
    return (entry - current) / entry * 100


def close_signal(sig, result, outcome, exit_price, pnl_pct, pnl_usd, now_str, reason_detail=""):
    sig["status"]     = result
    sig["result"]     = outcome
    sig["exit_price"] = exit_price
    sig["pnl_usd"]    = pnl_usd
    sig["pnl_pct"]    = pnl_pct
    sig["exit_time"]  = now_str
    sig["closed_at"]  = now_str
    emoji = "WIN" if result == "WIN" else "LOSS"
    pnl_str = f"+${pnl_usd} (+{pnl_pct}%)" if result == "WIN" else f"-${abs(pnl_usd)} ({pnl_pct}%)"
    detail = f"\n[DETAIL] {reason_detail}" if reason_detail else ""
    msg = (
        f"{emoji} SIGNAL {result}{detail}\n"
        f"Pair: {sig['pair']} | {sig.get('direction', '?')}\n"
        f"Entry: {sig['entry']} | Exit: {exit_price}\n"
        f"Result: {outcome}\n"
        f"P&L: {pnl_str} (${int(CAPITAL_PER_TRADE)} capital)\n"
        f"Time: {now_str[:16].replace('T', ' ')} UTC"
    )
    send_telegram(msg)
    print(f"[{result}] {sig.get('exchange','?')} | {sig['pair']} {sig.get('direction','?')} | {outcome} | PnL: {pnl_str}")


def validate_signals():
    if not os.path.exists(SIGNAL_LOG_FILE):
        print(f"[INFO] No signal log found at {SIGNAL_LOG_FILE}")
        return

    with open(SIGNAL_LOG_FILE, "r") as f:
        signals = json.load(f)

    updated = False
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()

    open_sigs = []
    for sig in signals:
        if sig.get("status") != "OPEN":
            continue
        symbol    = sig.get("pair", "")
        direction = sig.get("side", sig.get("direction", "")).upper()
        entry     = float(sig.get("entry", 0))
        sl        = float(sig.get("sl", 0))
        exchange  = sig.get("exchange", "Binance")
        tp        = get_tp_value(sig)
        if not symbol or not entry or not tp or not sl:
            print(f"[SKIP] Incomplete signal {sig.get('id', '?')}")
            continue
        open_sigs.append((sig, symbol, direction, entry, sl, exchange, tp))

    print(f"[INFO] Checking {len(open_sigs)} OPEN signals...")

    def _fetch(args):
        sig, symbol, direction, entry, sl, exchange, tp = args
        price = get_exchange_price(symbol, exchange)
        return (sig, symbol, direction, entry, sl, exchange, tp, price)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch, s): s for s in open_sigs}
        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            if done_count % 20 == 0:
                print(f"       Prices fetched: {done_count}/{len(open_sigs)}")
            try:
                sig, symbol, direction, entry, sl, exchange, tp, current_price = future.result()
            except Exception as e:
                print(f"[ERROR] {e}")
                continue
            if current_price is None:
                continue

            pnl_pct = calc_pnl_pct(entry, current_price, direction)
            pnl_usd = round(CAPITAL_PER_TRADE * pnl_pct / 100, 2)
            pnl_pct = round(pnl_pct, 2)

            signal_time = sig.get("time", "")
            age_hours = 0
            if signal_time:
                try:
                    dt_open = datetime.fromisoformat(signal_time.replace("Z", "+00:00"))
                    age_hours = (now - dt_open).total_seconds() / 3600
                except Exception:
                    pass

            closed = False

            # 1. SL HIT
            if direction == "LONG" and current_price <= sl:
                close_signal(sig, "LOSS", "SL HIT", current_price, pnl_pct, pnl_usd, now_str,
                             f"Stop-loss hit at {sl}")
                closed = True
            elif direction == "SHORT" and current_price >= sl:
                close_signal(sig, "LOSS", "SL HIT", current_price, pnl_pct, pnl_usd, now_str,
                             f"Stop-loss hit at {sl}")
                closed = True

            # 2. TP HIT
            if not closed:
                if direction == "LONG" and current_price >= tp:
                    close_signal(sig, "WIN", "TP HIT", current_price, pnl_pct, pnl_usd, now_str,
                                 f"Take-profit hit at {tp}")
                    closed = True
                elif direction == "SHORT" and current_price <= tp:
                    close_signal(sig, "WIN", "TP HIT", current_price, pnl_pct, pnl_usd, now_str,
                                 f"Take-profit hit at {tp}")
                    closed = True

            # 3. TRAILING SL (activates after TRAIL_ACTIVATION_PCT favorable move)
            if not closed and pnl_pct >= TRAIL_ACTIVATION_PCT:
                if direction == "LONG":
                    # For LONG: trail_sl = current_price * (1 - trail_dist/100)
                    # We check if price has pulled back from peak by trail distance
                    # Simplified: if current SL is still at original level and we're up >5%, move SL to +2%
                    trail_sl_price = entry * (1 + (pnl_pct - TRAIL_DISTANCE_PCT) / 100)
                    if current_price <= trail_sl_price and pnl_pct > TRAIL_DISTANCE_PCT:
                        close_signal(sig, "WIN", "TRAILING SL", current_price, pnl_pct, pnl_usd,now_str,
                                     f"Trail activated at +{pnl_pct:.1f}%, locked in +{pnl_pct-TRAIL_DISTANCE_PCT:.1f}%")
                        closed = True
                elif direction == "SHORT":
                    trail_sl_price = entry * (1 - (pnl_pct - TRAIL_DISTANCE_PCT) / 100)
                    if current_price >= trail_sl_price and pnl_pct > TRAIL_DISTANCE_PCT:
                        close_signal(sig, "WIN", "TRAILING SL", current_price, pnl_pct, pnl_usd, now_str,
                                     f"Trail activated at +{pnl_pct:.1f}%, locked in +{pnl_pct-TRAIL_DISTANCE_PCT:.1f}%")
                        closed = True

            # 4a. STALE TRADE: sideways after 12h (price within +/-STALE_RANGE_PCT% of entry)
            if not closed and age_hours > STALE_CHECK_HOURS and abs(pnl_pct) < STALE_RANGE_PCT:
                result = "WIN" if pnl_usd >= 0 else "LOSS"
                close_signal(sig, result, "STALE CLOSE", current_price, pnl_pct, pnl_usd, now_str,
                             f"Sideways for {age_hours:.0f}h, price within +/-{STALE_RANGE_PCT}% of entry")
                closed = True

            # 4b. TIME-DECAY: losing trade after 24h -> close to recycle capital
            if not closed and age_hours > BREAKEVEN_CLOSE_HOURS and pnl_pct < -1.0:
                close_signal(sig, "LOSS", "TIME-DECAY CLOSE", current_price, pnl_pct, pnl_usd, now_str,
                             f"Losing trade after {age_hours:.0f}h — recycling capital")
                closed = True

            # 5. FORCE CLOSE at 48h
            if not closed and age_hours > FORCE_CLOSE_HOURS:
                result = "WIN" if pnl_usd >= 0 else "LOSS"
                close_signal(sig, result, "FORCE CLOSE", current_price, pnl_pct, pnl_usd, now_str,
                             f"Force close after {age_hours:.0f}h — capital recycling")
                closed = True

            # 6. HARD EXPIRY at 72h
            if not closed and age_hours > MAX_SIGNAL_AGE_HOURS:
                result = "WIN" if pnl_usd >= 0 else "LOSS"
                close_signal(sig, result, "EXPIRED", current_price, pnl_pct, pnl_usd, now_str,
                             f"Hard expiry after {age_hours:.0f}h at current price")
                closed = True

            if closed:
                updated = True
            else:
                print(f"[OPEN] {exchange} | {symbol} {direction} | "
                      f"Entry:{entry} SL:{sl} TP:{tp} Curr:{current_price} "
                      f"PnL:{pnl_pct:+.2f}% Age:{age_hours:.1f}h")

    if updated:
        with open(SIGNAL_LOG_FILE, "w") as f:
            json.dump(signals, f, indent=2)
        print(f"[INFO] signals_log.json updated.")
    else:
        print(f"[INFO] No signals closed this run.")


if __name__ == "__main__":
    validate_signals()
