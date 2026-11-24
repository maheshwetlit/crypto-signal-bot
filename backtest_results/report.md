# Backtest report — v11-trend-pullback

Generated 2026-11-24 18:54 UTC · 28 KuCoin pairs · 15m candles · $1,000 per trade · costs 0.1% fee + 0.05% slippage per side

In-sample: 2026-05-28 → 2026-09-13 · Out-of-sample: 2026-09-13 → 2026-11-24


## Results
| set | trades | trades_per_day | win_rate | avg_R | per_trade_usd | net_usd | profit_factor | max_dd_usd |
|---|---|---|---|---|---|---|---|---|
| In-sample | 1011 | 9.39 | 32.4 | -0.199 | -1.22 | -1229.4 | 0.78 | -2481.2 |
| OUT-OF-SAMPLE | 849 | 11.86 | 35.1 | -0.237 | -2.03 | -1724.6 | 0.67 | -2084.4 |
| OOS longs | 243 | 3.56 | 29.6 | -0.122 | -0.59 | -142.6 | 0.92 | -535.5 |
| OOS shorts | 606 | 8.47 | 37.3 | -0.283 | -2.61 | -1582.0 | 0.55 | -1699.3 |


## Versus random entries (same exits & costs)
| set | strategy_avg_R | random_avg_R | random_95th_pct_R | beats_95pct_of_random |
|---|---|---|---|---|
| In-sample | -0.199 | -0.393 | -0.345 | True |
| OUT-OF-SAMPLE | -0.237 | -0.355 | -0.293 | True |


## By month
| month | trades | trades_per_day | win_rate | avg_R | per_trade_usd | net_usd | profit_factor | max_dd_usd |
|---|---|---|---|---|---|---|---|---|
| 2026-05 | 16 | 5.49 | 18.8 | -0.585 | -6.0 | -96.0 | 0.16 | -74.3 |
| 2026-06 | 412 | 13.95 | 41.3 | 0.022 | 0.86 | 353.2 | 1.16 | -994.6 |
| 2026-07 | 243 | 7.9 | 22.2 | -0.563 | -4.23 | -1027.3 | 0.34 | -1017.3 |
| 2026-08 | 209 | 6.87 | 32.1 | -0.089 | -0.27 | -55.5 | 0.95 | -257.6 |
| 2026-09 | 356 | 11.94 | 31.2 | -0.19 | -1.33 | -472.4 | 0.8 | -655.6 |
| 2026-10 | 308 | 10.14 | 31.5 | -0.355 | -3.32 | -1021.7 | 0.45 | -1024.2 |
| 2026-11 | 316 | 13.76 | 39.2 | -0.218 | -2.01 | -634.4 | 0.65 | -781.4 |


## Exit reasons
| reason | n | avg_R |
|---|---|---|
| BE | 516 | -0.023 |
| SL | 788 | -1.349 |
| TIME | 150 | -0.001 |
| TP | 406 | 1.659 |


## Pairs (net % summed)
Worst: ENA/USDT -29.6%, PIEVERSE/USDT -29.4%, AAVE/USDT -19.1%, UNI/USDT -18.6%, ETH/USDT -16.4%

Best: ELSA/USDT -4.5%, BTC/USDT -3.0%, SEI/USDT -0.5%, TRUMP/USDT 4.0%, NEAR/USDT 4.1%


## Verdict
**FAIL** — out-of-sample does not show a robust edge after costs. Do NOT trade this with real money. Do not tune parameters on the OOS period.


## Robustness (IN-SAMPLE only — look for a plateau, not a peak)
| variant | trades | trades_per_day | win_rate | avg_R | per_trade_usd | net_usd | profit_factor | max_dd_usd |
|---|---|---|---|---|---|---|---|---|
| baseline | 1011 | 9.39 | 32.4 | -0.199 | -1.22 | -1229.4 | 0.78 | -2481.2 |
| TP 1.5R | 1023 | 9.5 | 38.0 | -0.214 | -1.4 | -1432.6 | 0.75 | -2547.2 |
| TP 2.5R | 1005 | 9.33 | 29.7 | -0.179 | -1.07 | -1073.9 | 0.81 | -2408.8 |
| ADX max 35 | 907 | 8.42 | 33.0 | -0.179 | -1.05 | -951.9 | 0.81 | -2121.1 |
| ADX max 50 | 1081 | 10.04 | 32.3 | -0.21 | -1.36 | -1469.5 | 0.76 | -2710.4 |
| ext max 0.75 ATR | 694 | 6.47 | 33.7 | -0.16 | -0.89 | -620.8 | 0.84 | -1439.5 |
| ext max 1.5 ATR | 1497 | 13.9 | 32.4 | -0.219 | -1.5 | -2245.8 | 0.75 | -3674.7 |
| time stop 4h | 1022 | 9.49 | 32.3 | -0.215 | -1.48 | -1514.1 | 0.73 | -2551.7 |
| time stop 8h | 1008 | 9.36 | 33.0 | -0.185 | -1.06 | -1068.0 | 0.81 | -2293.1 |
| no BTC filter | 1230 | 11.42 | 31.5 | -0.227 | -1.63 | -2009.3 | 0.72 | -3128.7 |
