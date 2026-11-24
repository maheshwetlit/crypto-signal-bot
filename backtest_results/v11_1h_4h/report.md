# Backtest report — v11-trend-pullback

Generated 2026-11-24 18:59 UTC · 28 KuCoin pairs · 1h candles, 4h trend · max risk 5.0% · $1,000 per trade · costs 0.1% fee + 0.05% slippage per side

In-sample: 2026-05-28 → 2026-09-13 · Out-of-sample: 2026-09-13 → 2026-11-24


## Results
| set | trades | trades_per_day | win_rate | avg_R | per_trade_usd | net_usd | profit_factor | max_dd_usd |
|---|---|---|---|---|---|---|---|---|
| In-sample | 372 | 3.59 | 27.7 | -0.315 | -5.16 | -1919.5 | 0.55 | -2612.5 |
| OUT-OF-SAMPLE | 285 | 4.12 | 32.6 | -0.128 | -2.77 | -789.5 | 0.73 | -1138.1 |
| OOS longs | 95 | 2.01 | 20.0 | -0.262 | -6.18 | -587.4 | 0.54 | -680.7 |
| OOS shorts | 190 | 2.74 | 38.9 | -0.061 | -1.06 | -202.1 | 0.88 | -616.8 |


## Versus random entries (same exits & costs)
| set | strategy_avg_R | random_avg_R | random_95th_pct_R | beats_95pct_of_random |
|---|---|---|---|---|
| In-sample | -0.315 | -0.276 | -0.205 | False |
| OUT-OF-SAMPLE | -0.128 | -0.222 | -0.122 | False |


## By month
| month | trades | trades_per_day | win_rate | avg_R | per_trade_usd | net_usd | profit_factor | max_dd_usd |
|---|---|---|---|---|---|---|---|---|
| 2026-05 | 23 | 10.82 | 30.4 | -0.414 | -6.65 | -153.0 | 0.23 | -198.9 |
| 2026-06 | 134 | 4.49 | 36.6 | -0.074 | -0.05 | -7.3 | 0.99 | -853.3 |
| 2026-07 | 81 | 2.65 | 18.5 | -0.668 | -9.21 | -746.0 | 0.19 | -741.7 |
| 2026-08 | 80 | 2.63 | 23.8 | -0.264 | -7.1 | -567.7 | 0.4 | -613.7 |
| 2026-09 | 116 | 3.88 | 24.1 | -0.254 | -5.95 | -689.9 | 0.59 | -756.4 |
| 2026-10 | 109 | 3.63 | 30.3 | -0.297 | -5.65 | -616.2 | 0.43 | -618.9 |
| 2026-11 | 114 | 5.15 | 39.5 | 0.027 | 0.62 | 71.1 | 1.07 | -336.0 |


## Exit reasons
| reason | n | avg_R |
|---|---|---|
| BE | 166 | -0.032 |
| SL | 312 | -1.2 |
| TIME | 51 | -0.067 |
| TP | 128 | 1.794 |


## Pairs (net % summed)
Worst: ENA/USDT -27.6%, SUI/USDT -23.9%, BTC/USDT -18.9%, SEI/USDT -18.6%, ASTER/USDT -18.4%

Best: ARB/USDT -3.5%, AVAX/USDT -3.0%, TAO/USDT 4.1%, ZEC/USDT 4.4%, XRP/USDT 5.4%


## Verdict
**FAIL** — out-of-sample does not show a robust edge after costs. Do NOT trade this with real money. Do not tune parameters on the OOS period.


## Robustness (IN-SAMPLE only — look for a plateau, not a peak)
| variant | trades | trades_per_day | win_rate | avg_R | per_trade_usd | net_usd | profit_factor | max_dd_usd |
|---|---|---|---|---|---|---|---|---|
| baseline | 372 | 3.59 | 27.7 | -0.315 | -5.16 | -1919.5 | 0.55 | -2612.5 |
| TP 1.5R | 373 | 3.6 | 31.1 | -0.327 | -5.69 | -2121.4 | 0.51 | -2745.6 |
| TP 2.5R | 371 | 3.58 | 26.4 | -0.278 | -4.46 | -1654.1 | 0.61 | -2505.1 |
| ADX max 35 | 325 | 3.14 | 26.2 | -0.341 | -5.71 | -1854.3 | 0.51 | -2162.6 |
| ADX max 50 | 407 | 3.92 | 27.8 | -0.32 | -5.33 | -2168.6 | 0.55 | -2895.5 |
| ext max 0.75 ATR | 255 | 2.49 | 27.8 | -0.291 | -4.83 | -1231.6 | 0.57 | -1668.6 |
| ext max 1.5 ATR | 519 | 5.0 | 28.9 | -0.276 | -4.45 | -2310.3 | 0.63 | -3312.4 |
| time stop x0.67 | 375 | 3.62 | 27.5 | -0.326 | -5.41 | -2028.8 | 0.53 | -2785.2 |
| time stop x1.33 | 371 | 3.58 | 28.6 | -0.303 | -4.96 | -1838.4 | 0.58 | -2529.7 |
| no BTC filter | 448 | 4.17 | 27.5 | -0.337 | -5.86 | -2626.9 | 0.51 | -3175.7 |
