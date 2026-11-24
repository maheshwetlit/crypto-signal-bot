# Daily strategy backtest — v12-daily-dip — phase: DEV

Generated 2026-11-24 19:59 UTC · Hyperliquid daily candles · 234 coins (top 30 by 30-day volume traded each day) · start $1,000

Costs: entry maker 0.015%, exit taker 0.045% + 0.05% slippage, funding 0.03%/day always paid

Data: 2023-01-01 → 2025-06-30 · Judged period: 2023-01-01 → 2025-06-30 (data after the cutoff was NOT loaded)


## Trades
| set | trades | win_rate | avg_trade_pct | avg_R | profit_factor | net_usd | months_profitable_pct | avg_days_held |
|---|---|---|---|---|---|---|---|---|
| ALL | 233 | 66.5 | 3.432 | 0.088 | 1.42 | 215.2 | 65.4 | 5.9 |
| LONG | 125 | 64.8 | 5.271 | 0.154 | 1.98 | 202.1 | 73.9 | 6.2 |
| SHORT | 108 | 68.5 | 1.305 | 0.011 | 1.04 | 13.1 | 52.4 | 5.6 |


## Account ($1,000 start, your slot rule, 1% risk, ≤3x)
| start_equity | end_equity | return_pct | cagr_pct | max_drawdown_pct | avg_open_positions |
|---|---|---|---|---|---|
| 1000.0 | 1215.2 | 21.5 | 8.1 | -9.9 | 1.52 |


## By year
| year | trades | win_rate | avg_trade_pct | avg_R | profit_factor | net_usd | months_profitable_pct | avg_days_held |
|---|---|---|---|---|---|---|---|---|
| 2023 | 60 | 68.3 | 2.152 | 0.087 | 1.36 | 51.0 | 62.5 | 6.0 |
| 2024 | 111 | 59.5 | 2.624 | 0.034 | 1.13 | 35.0 | 58.3 | 6.0 |
| 2025 | 62 | 77.4 | 6.119 | 0.186 | 2.29 | 129.2 | 83.3 | 5.7 |


## Exit reasons
| reason | n | win_rate | avg_trade_pct |
|---|---|---|---|
| BOUNCE | 176 | 86.9 | 9.037 |
| STOP | 34 | 0.0 | -17.303 |
| TIME | 23 | 8.7 | -8.805 |


## Versus random timing (same coins, same trend filter, same exits & costs)
| strategy_avg_trade_pct | random_avg_trade_pct | random_95th_pct | strategy_win_rate | random_win_rate | beats_95pct_of_random |
|---|---|---|---|---|---|
| 3.432 | 0.29 | 1.815 | 66.5 | 66.6 | True |


## Checks
- ✅ trades >= 50
- ✅ win rate >= 55%
- ✅ profit factor >= 1.3
- ✅ >= 60% months profitable
- ✅ net profit > 0
- ✅ beats random timing


## Robustness (DEV only — a real edge is a plateau, not a single lucky setting)
| variant | trades | win_rate | avg_trade_pct | avg_R | profit_factor | net_usd | months_profitable_pct | avg_days_held | max_dd_pct |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 233 | 66.5 | 3.432 | 0.088 | 1.42 | 215.2 | 65.4 | 5.9 | -9.9 |
| RSI 15/85 | 146 | 63.0 | 1.329 | 0.011 | 1.03 | 10.1 | 50.0 | 6.1 | -12.5 |
| RSI 25/75 | 284 | 62.3 | 1.381 | 0.007 | 1.01 | 6.8 | 50.0 | 6.4 | -19.0 |
| stop 2.0 ATR | 238 | 65.1 | 3.255 | 0.104 | 1.4 | 262.3 | 61.5 | 5.5 | -11.0 |
| stop 3.0 ATR | 229 | 66.4 | 3.554 | 0.077 | 1.46 | 184.3 | 65.4 | 6.3 | -9.9 |
| limit offset 0 | 297 | 65.3 | 2.18 | 0.068 | 1.31 | 208.1 | 53.8 | 5.7 | -11.0 |
| limit offset 0.5 | 173 | 71.7 | 4.61 | 0.137 | 1.72 | 258.6 | 65.4 | 6.2 | -9.1 |
| time stop 7d | 235 | 63.4 | 3.071 | 0.073 | 1.34 | 175.1 | 61.5 | 5.4 | -12.2 |
| time stop 14d | 233 | 68.2 | 3.536 | 0.092 | 1.44 | 227.2 | 65.4 | 6.2 | -9.5 |
| exit lookback 3 | 241 | 66.8 | 3.237 | 0.088 | 1.6 | 227.8 | 69.2 | 4.2 | -8.5 |
| exit lookback 7 | 218 | 63.3 | 3.093 | 0.085 | 1.33 | 190.4 | 65.4 | 7.2 | -12.7 |
| top 20 coins | 186 | 67.2 | 3.451 | 0.093 | 1.46 | 180.2 | 57.7 | 5.9 | -9.4 |
| top 50 coins | 277 | 65.0 | 2.878 | 0.075 | 1.35 | 216.8 | 73.1 | 6.1 | -9.0 |
| longs only | 131 | 65.6 | 5.208 | 0.156 | 2.02 | 220.3 | 69.6 | 6.2 | -5.1 |
| funding 0.06%/day | 233 | 65.2 | 3.255 | 0.079 | 1.37 | 188.5 | 61.5 | 5.9 | -10.4 |


## Verdict
**PASS** (dev)
