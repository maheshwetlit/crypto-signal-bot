# Hermes v11 — trend pullback (paper trading)

## Why v10 was replaced
An audit of all 8,635 closed v10 trades (Jun–Nov 2026, rebuilt from git history) showed:

- Profit factor 1.01 and +$598 before fees. That becomes about **−$8k to −$17k after fees** at ~51 trades/day.
- The highest-scored signals were the worst ones: score 95+, ADX > 40 and volume > 3× lost money. The bot was chasing exhausted moves.
- Win rate looked fine (53%), but the 0.9% target versus a 2% stop meant losses were bigger than wins.
- Results were overstated:
  - exits were checked from 10-minute price snapshots with the target checked first;
  - 28% of trades vanished from the log because two workflows overwrote each other;
  - signals were computed from the still-forming candle;
  - "grinding" averaged down into losers.

## v11 rules (`strategy.py`, shared by bot and backtest)
| | Rule |
|---|---|
| Trend | 1h close > EMA50 > EMA200 (shorts: mirror). BTC 1h above/below its EMA50 in the same direction |
| Setup | 15m EMA20 > EMA50; price pulled back to within 0.25 ATR of EMA20 in the last 4 bars without breaking EMA50 |
| Trigger | Closed 15m candle closes beyond the previous candle's high/low, in trend direction |
| Fake-trend guards | Close ≤ 1 ATR from EMA20 · ADX 18–40 · volume 0.8–3× median · RSI 45–65 (long) / 35–55 (short) · ATR ≥ 0.3% · live: skip if price already ran > 0.5R |
| Stop | Below/above the 5-bar swing − 0.2 ATR, risk 0.6–3% |
| Target | 2R; breakeven after +1R; time stop 6h |
| Costs | 0.10% fee + 0.05% slippage per side, always included |
| Limits | 1 open per pair, 10 total, cooldown after each trade |

## How to judge it (and not fool yourself)
1. Run **Actions → Backtest strategy**. The report lands in `backtest_results/report.md`.
2. Only the **out-of-sample** block counts. It must be positive after costs, have PF ≥ 1.15 over at least 100 trades, and beat 95% of random-entry runs.
3. The robustness table should show a plateau. If only one exact setting works, it's noise.
4. **Never tune parameters on the out-of-sample period.** If you change rules, re-run the whole report and treat the result skeptically.
5. Paper-trade forward 4–8 weeks. Real money only if the live paper results resemble the out-of-sample backtest.

The v10 log is kept in `archive/signals_log_v10_final.json`.
