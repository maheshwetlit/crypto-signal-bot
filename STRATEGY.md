# Hermes v12 — daily dip-in-trend (paper trading, Hyperliquid data)

## History
- **v10** (15m breakout scalper, KuCoin): about +$598 before fees over 8,635 trades, profit factor 1.01. That is about −$8k to −$17k after fees, so it lost money.
- **v11** (15m / 1h trend pullback): failed its honest backtests (out-of-sample profit factor 0.67 and 0.73). See `backtest_results/report.md` and `backtest_results/v11_1h_4h/`.
- **Lesson:** at 15m–1h, trading costs are a large share of each trade. v12 moves to daily candles, where each move is many times the cost.

## v12 rules (`strategy_daily.py`, shared by bot and backtest)
| | Rule |
|---|---|
| Universe | Each day: perps with ≥120 days of history; trade the top 30 by 30-day average dollar volume |
| Long setup | close > SMA100 (uptrend), 20-day return > 0 (strength), RSI(3) < 20 (dip) |
| Short setup | Mirror: close < SMA100, 20-day return < 0, RSI(3) > 80 |
| Entry | Limit order for the next day at close ∓ 0.25 ATR(14); expires after one day |
| Stop | 2.5 ATR beyond the limit price |
| Exit | Close above the highest close of the previous 5 days (shorts: mirror), or after 10 days |
| Sizing | 1% of equity at risk per trade; total ≤ 3× equity; one position ≤ 1× equity |
| Slots | Start 4; a win adds one, a loss removes one; kept between 3 and 6 |
| Costs | Maker fee on entry, taker fee + 0.05% slippage on exit, funding 0.03%/day always paid |
| Safety | New entries stop at a 25% drawdown from peak (to be set to 1.5× the backtest's worst drawdown) |

## How it is judged
1. **Dev phase.** `Actions → Backtest daily strategy`, phase `dev`. It only sees data before 2025-07-01 and includes a robustness grid.
2. **Final phase.** Run once on 2025-07-01 → today, a period nobody has looked at. To pass it needs:
   - at least 50 trades;
   - win rate ≥ 55%;
   - profit factor ≥ 1.3;
   - at least 60% of months profitable;
   - net profit after all costs;
   - and it must beat random-timing entries.
3. **If it fails,** don't adjust settings and re-run on the same period.
4. **If it passes,** paper-trade for 4–8 weeks before any real order code is enabled.

## Files
- `strategy_daily.py`: rules, sizing and cost model.
- `hl_data.py`: Hyperliquid public data. This is the only file tied to the exchange; a Lighter adapter would be a sibling file.
- `backtest_daily.py`: portfolio backtest with your slot rule, the random baseline and the robustness grid.
- `crypto_signal_bot.py`: hourly paper bot with Telegram messages.
