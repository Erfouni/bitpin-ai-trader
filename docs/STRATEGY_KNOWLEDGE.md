# Base rates for the Bitpin bot (Bitpin hourly data 2024-04..2026-09; one-year replan 2026-09-26)

TRAIN = 2024-04 to 2026-04 (tuning), HOLDOUT = the unseen last 150 days, LAST60 = the last 60 days, Y1 = 450
overlapping 365-day windows (starts 2024-06-28..2025-09-22, about 2.5 independent years, 1% per round trip), Y5 =
the tokenized-asset study (2024-01..2026-09). The competition runs ONE YEAR; the benchmark is holding USDT_IRT. A row is evidence only when its CONDITION holds now (cite the context field that shows it) and its
horizon covers the plan's horizon_hours; a row of a shorter horizon is not a substitute. Coin-vs-USDT rows are in
USDT terms; net = after a 0.9% round trip; n = coin-days unless stated.

| id | condition (check it in the context) | horizon | result | sample |
|---|---|---|---|---|
| B1 | hold USDT_IRT (the benchmark) | 720 h | HOLDOUT mean +8.4% a month, median +3.8%, worst 10% -5.9% (+49% over the 150 days; +150% in the 720 days before). One year: x1.81 mean / x1.75 median (p10 x1.51, p90 x2.16) in Y1; 365 d +66% median, never negative in 634 windows (Y5); log drift +0.51..0.56 a year, vol 27%. After a 6% drop it rebounded about +2% within 24 h (TRAIN): toman after a fall lost | 150 d; 450 windows |
| B2 | any top-4 coin vs holding USDT, no condition | 720 h | 30 d excess return median -1.0% TRAIN / +0.3% HOLDOUT; worst 10% about -20%, best 10% +25 to +30%: P(a coin beats USDT over 30 d) about 0.5, fat-tailed. Last month's winner does not predict next month's (rank correlation -0.05 TRAIN, -0.17 HOLDOUT); the 7-day return predicts next week with rank IC -0.04 / -0.11 (majors, n 2952 / 572). LAST60: top-4 beat USDT by about +27% per 30 d - one or two months, not a rate | 870 d |
| B3 | the coin rose 15% or more WITHIN 48 h (ret_usdt[0], ret_usdt[1] or dd48 show it; a 7-day rise does not qualify) | 168 h | next 7 d about +5% per event in the HOLDOUT, mixed per cluster: a rise alone is not a sell signal, and with that sample it is not a buy signal either. After a 7-day rise above 17%: majors +7.7% mean / +1.9% median TRAIN (n 146), +0.9% HOLDOUT (n 27); all 16 coins HOLDOUT -3.7% mean, up in 31% (n 142) | few clusters |
| B4 | majors fell 8% or more (dd48 <= -8 or ret_usdt[1] <= -8) | 168 h | next 7 d +1.4% TRAIN, -3.0% HOLDOUT (mean-reverting turned trending). Buying at -15% below the 48 h high on the next hourly close: -0.2% (CI -2.3 to +1.9%): no edge. A pullback in an uptrend (above EMA50 and EMA200 on 4h, dd48 -12..0): 7 d net -2.4% TRAIN (n 469, hit 43%) / +2.4% HOLDOUT (n 140, hit 57%); 30 d net -1.6% TRAIN / 0.0% HOLDOUT (n 81): the sign flips between periods. Waiting for a model call before buying a crash kept only half of the rebound | TRAIN + HOLDOUT |
| B5 | a resting maker buy 20% below the 48 h high in a liquid major: the crash ladder's fill (dd48 <= -20) | 48-72 h | TRAIN +10% per crash cluster (90% CI +7 to +13%, 23 clusters; stops hit 22% of fills, 71% reached the half-drop target in a median 45-56 h); the -25% level +8.8% (CI +1.7 to +14.7%). HOLDOUT: NO -20% fill in the four majors; the one fill among 9 majors (ADA, 2026-06-05) lost 14%. A regime bet on sharp crash-and-rebound markets, run by code | 23 clusters TRAIN, 1 HOLDOUT |
| B6 | the coin rose 30% or more within 24 h (pump_guard) - buying after it | 24-168 h | 24 h later mean -6%, median -8%; 7 d later median -13%; only 25-30% ended higher, about 1% doubled again; half of the gain gone within 11-17 h; round trip on those thin books 5-8%; no robust rule in about 60k tested variants, every in-sample winner failed out of sample | 1031 events, 547 markets, 400 d |
| B7 | mechanical strategies tuned on TRAIN, tested once on the HOLDOUT (30 d windows) | 720 h | Donchian trend following on coin/USDT, small sleeve: median +4.6% vs +3.7% for USDT, total +65% vs +49% from 3 lucky trades, worst 10% -7%; momentum rotation among 16 coins: median +1.4%, worst 10% -12%; gold/BTC/ETH risk parity: worst 10% -14%, maxDD 27%; coin-vs-coin or vs-gold relative value: worst 10% -13% (the train edge was static gold exposure); dip buying after a 17.5% two-day crash: 1 episode, -6.8% to +3% per trade. None beat USDT robustly: only hold_usdt is holdout_verified. Per setup (7 d / 30 d net, TRAIN then HOLDOUT): trend continuation above all three 4h EMAs +0.8 / -1.6 and +5.0 / -2.4 (n 1478 / 299); a 20-day-high breakout with volume +2.3 / +4.2 and +7.9 / 0.0 (n 80 / 19) | 150 d; 715 + 143 d |
| B8 | a timing call on majors at a 1.0% round trip | 4-24 h | break-even hit rate 95-117% (impossible) at 4 h, 76-88% at 12 h, 68-75% at 24 h; hourly deciding lost 2-5 points a month to noise trades versus daily; twice a day instead of once: under 0.3 points | simulation |
| B9 | volatility (sig_d) and small caps | 720 h | P(an hourly close 12% below the entry within 7 d) by realised daily sigma: sig_d < 2.5: 4% TRAIN (n 998) / 3% HOLDOUT (n 798); 2.5-4: 13% / 10% (n 3665 / 858); 4-6: 28% / 17% (n 3629 / 443); above 6: 34% / 28% (n 3516 / 189); the expected 7-day return does not rise with sigma (rank IC -0.05). Small caps (PEPE, SHIB, ARB, NEAR, SUI) had 50-85% drawdowns over 900 d; PAXG was a strong diversifier in 2024-25 but did not beat USDT in the 150-day HOLDOUT (B11 has the one-year view) | 900 d; 150 d |
| B10 | the 84-day trend state of BTC/USDT (macro.btc_trend84: on = above its close 84 days ago; only the 63-84 day band worked as a filter, 28-42 d lost, 126 d was neutral) | 720 h | Y1 path result, 100% in the four majors only while the state is on (else USDT): one-year ratio to USDT mean 1.344 [bootstrap 1.06-1.69], median 1.136, below USDT in 29% of windows, 9 switches a year (cost included) - the only policy whose bootstrap range excludes 1.0; at 65% coin 1.234 / 1.110 / 26%. Day by day (equal-weight majors, no cost): next 30 d excess over USDT with the state ON mean +6.3%, median +0.8%, P(win) 0.51 (n 499 days); OFF +13.7% / +11.0% / 0.82 (n 286); HOLDOUT ON -10.3% / -16.0% / 0.26 (n 57), OFF +26.5% / +23.5% / 0.99 (n 93). The lows of 2025-04 and 2026-04 sit inside OFF periods (the state turns on 84 days after a low): the path gain comes from being out in the middle of the two bears, not from better entry days. A regime read, not a rule: the code caps nothing on it | 2.5 years, 2 bear markets |
| B11 | gold (PAXG / XAUT / GLDON) vs USDT | 720 h | 30 d median +2.3% (p10 -4.5%, p90 +10%, negative 31%); 90 d +8% (negative 19%); 365 d +42% (p10 +23%, never negative in 634 windows) - a 2024-26 regime, not a law; ann vol 26%, maxDD -27%; weekly correlation with BTC 0.09-0.22; momentum-chasing is neutral. Y5 allocation: 30% of a 100%-coin book moved into gold (with the B10 state): mean 1.426 -> 1.465, median 1.212 -> 1.311, p10 0.80 -> 0.92, below USDT 25% -> 17%, a 30% toman drawdown 70% -> 0% of windows; 30% out of USDT into gold at 65% coin: +0.10 mean and median, better in 100% of windows; a flat-gold year with a crypto bull costs 10-25 points. Round trip 0.9% + 0.1-0.2% spread; XAUT 0.2% cheaper than PAXG, spread 0.11%, depth 11-13 bn toman; a -12% hourly stop fires in 2.1% of weeks | 634 windows; 452 windows |
| B12 | tokenized US stocks / ETFs / oil / gas / copper on Bitpin (cls, us_session) | 720 h | Underlyings 2024-01..2026-09: US mega-cap basket 30 d median +2.9% (negative 30%), 365 d +25% (p10 +13%); NVDA 365 d +38%; SPY 365 d +16%, vol 16%; oil (USO) 30 d +1% (negative 46%), 365 d +3% (negative 41%), vol 38%. The Bitpin token is the underlying plus noise: premium std 1.9-2.8% for xStocks, 2.9% USOON; hourly noise 1.7-2.2% (the share itself 1.0%); weekly correlation with the share 0.78-0.89, daily only 0.3-0.6; weekend moves (std 2.4-3.5%) predict the Monday gap with correlation 0.1-0.4: noise. A move under 3% in such a token is noise: judge it by the underlying's outlook, buy only while us_session is open, as a maker order near the reference. Round trip 1.5-2.5% + 0.6-5.3% spread (GOOGLX 0.59%, AAPLX 2.3%, AMZNX 5.3%); the Ondo listings of 2026-09 have 8.6-17% spreads. COINX / CRCLX / MSTRON / HOODX are crypto beta (weekly correlation with BTC 0.68 / 0.55, 365 d -50% / -40%): the crypto cluster, not a hedge. A gold/oil/stock mix (1.420 / 1.305) trailed gold alone (1.465 / 1.311) | 306-445 d per token; 452 windows |

ONE-YEAR ALLOCATION (Y1, 450 365-day windows, quarterly rebalance; ratio = final toman value / holding USDT)
| policy | mean | median | P(<USDT) | note |
|---|---|---|---|---|
| 100% USDT_IRT | 1.000 | 1.000 | - | x1.81 mean, x1.75 median in toman |
| four majors 100% | 1.236 [0.74-1.80] | 0.814 | 0.67 | every 2025 start lost (0.50-0.86); the mean is 2024-H2 starts (2024-Q3 2.42, 2025-Q3 0.50) |
| four majors 65% / 35% | 1.166 / 1.093 | 0.911 / 0.968 | 0.66 / 0.61 | top-9 coins 65% with drift rebalancing 1.161 / 1.026 / 0.46 |
| four majors 100% + B10 state | 1.344 [1.06-1.69] | 1.136 | 0.29 | p10 x1.60 toman (USDT p10 x1.51), p90 x3.69; 2025-Q1 / Q2 / Q3 starts 1.12 / 1.14 / 0.79 |
| 65% majors with a -12% stop (fixed / weekly reset) | 0.963 / 0.929 | 0.769 / 0.750 | 0.69 | vs 1.166 without: every stop tested (-12 / -20 / -30) was net negative, 8.6-8.8%/yr of fees: churn |
| 65% majors, 30% toman halt (freeze) | 1.142 | 0.906 | 0.66 | fired in 41% (65% coin) to 100% (100% coin) of windows and froze the B10 policy (1.344 -> 1.114); a 50% halt never fired: the bot's halt is 50% in v3 |
| the v2 code (65% + weekly stop + 30% halt) | 0.999 | 0.822 | 0.68 | halted in 100% of windows: equal to doing nothing |
Kelly (four majors, daily USDT terms): f* 0.79 on 898 d (half 0.40), 0.63 TRAIN, 2.1 HOLDOUT, NEGATIVE on the last
365 d: the drift is not estimable to its own size. Rebalance cadence is second-order (within 0.03). The four majors
are one bet: pairwise correlation 0.74, 1.1-1.3 effective bets; with 30% gold 1.6-1.8.

SETUPS (the evidence a plan's setup must meet in the context, USDT terms; the rows that price it; the no-edge
probability is l / (g + l) in every case and the row says whether an edge exists at all)
| setup | required evidence (full-detail coins unless stated) | rows and what they allow |
|---|---|---|
| dip_in_uptrend | ema_dev_pct[1] > 0 and ema_dev_pct[2] > 0; dd48 between -4 and -12 or rsi4h < 45 | B4 (the sign flipped between TRAIN and HOLDOUT: p starts at l/(g+l)); if majors fell 8%+, B4's crash line applies and p goes below it |
| trend_continuation | all three ema_dev_pct > 0; pos30 >= 0.8; ret_usdt[1] > 0; not pump_guard | B7 (net negative in the HOLDOUT at 7 and 30 d; rotation rejected: p starts at l/(g+l), above it only with a cited catalyst); B3 only when the rise was 15%+ within 48 h, and it is not a buy signal |
| breakout | d30h >= -1 and pos30 = 1 (at the 30-day high); vol_ratio >= 1.5 | B7 (breakout: n 80 / 19, 3 lucky Donchian trades): p starts at l/(g+l) |
| crash_rebound | dd48 <= -20 in a liquid major | B5: the crash ladder's job, run by code; a manual buy must argue B4 away, p at most l/(g+l) |
| relative_strength | ret_usdt[1] and ret_usdt[2] (7 d, 30 d) above BTC's (macro.btc_usd) with a named, dated catalyst | B2, B7 (rotation rejected; the cross-sectional 7-day rank has no next-week edge, IC -0.01 TRAIN / -0.08 HOLDOUT): p starts at l/(g+l) |
| mean_reversion | rsi4h < 30 with ema_dev_pct[2] > 0 | B4 (dips did not revert in the HOLDOUT; rsi4h < 35 next 7 d -0.6%, n 209): p below l/(g+l) unless a catalyst is cited |
| macro_hedge | gold / silver (cls gold, silver) on B11; a stock / ETF / oil token (cls us_stock, us_etf, bond, oil, gas, copper) on B12 with us_session open and its underlying's outlook | B11 (gold: a positive one-year row, the only sleeve that raised mean, median and p10 together); B12 (the underlying's rate minus the token's noise and spread) |
| other | name the row used, or "none" | p at most l/(g+l) |
A coin shown as a compact row (r_usdt, rsi4h, dd48, sig_d, d30h, beta, sp, d1_m only) cannot show 4h evidence: it can
only be relative_strength, macro_hedge or other. sig_d prices the invalidation odds (B9); beta_btc and
portfolio.effective_bets say whether a second coin adds a bet or only size.

Stops and targets (study 06, 2026-09-29: an entry every day at 19:00 Tehran, 168 h plans, BTC/ETH/SOL/XRP in
USDT terms, 1% cost, TRAIN n 3004 / HOLDOUT n 600): an invalidation below a support in sup, below don20_4h[0] or a
fixed 5% did the same on average (within 0.3 points per plan, the sign flipping between TRAIN and HOLDOUT); the
bare 2 x atr4h_pct minimum was stopped out in more than half of the plans and did worst; any of them cut the
worst plan from about -26..-31% to -10..-19% at a similar mean. A take_profit_usdt at 2 x l or just below the next
resistance in res did no better than holding to the horizon (-0.5 to -1.3 points per plan in three of four
samples, about 0 in the fourth): res is where the price may stall, not a cap on g.

Holding and re-entry (study 07, 2026-10-03; BTC/ETH/SOL/XRP setup entries, SUP invalidations, 111 overlapping
90-day windows from USDT, 1% round trip; mean per window TRAIN / HOLDOUT): re-testing a held coin every day on the
entry evidence: 23 round trips a coin-year, +7.6% / -2.4%; at plan expiry: 15, +8.5% / +0.1%; holding to the
invalidation: 8, +12.1% / +6.5% (buy and hold +11.6% / +9.6%), its gain from the big trends (it trailed daily
re-testing in 56% of the TRAIN windows); raising the invalidation weekly lost that gain; a 72 h pause after a sale
helped only a churning policy; an ev margin of +1 / +2% was mixed. At a 1.8% round trip holding led by 7.7 / 13.1.

Probability from data (study 08; 3000 coin-days, SUP invalidation, target 2.5 x l, p0 0.286): a model fitted on
TRAIN from the context's technical fields predicted the target-before-invalidation within 720 h WORSE than p0 in
the HOLDOUT (Brier skill -0.05; -0.08 with 12-week / 50- / 200-day trend fields). Setup days reached the target
first in 31% of TRAIN but 14% of HOLDOUT cases (all days 28%): a technical reading alone does not justify p above
p0. Within 168 h a 2.5 x l target came first in 10% of cases (p0 says 29%).

Lessons: No edge, no trade: without a view whose expected gain clearly beats the round-trip cost the capital stays
in USDT_IRT; with one, size it - holding USDT is itself a view (B1), not a safe default, and over a year it is the
benchmark, not a refuge. Measure coin trends in USDT terms, never in raw IRT. Returns from coin bets are concentrated
in a few big moves; most timing signals were noise after fees, so prefer few, high-conviction positions and hold each
for the horizon of its plan: holding 30 days beat holding 7 days in Y1; every avoided round trip is worth about 0.5
points a year.
Chasing coins that just pumped 30%+ has lost money on Bitpin in every month studied; do not buy a
  coin because it just spiked (B6). Sharp crashes in liquid coins partially reverted in TRAIN but not in the
HOLDOUT (B4, B5): the crash ladder, not a decision, buys them. A -12% stop is opt-in per position in v3 (every
tested stop was net negative over a year: the Y1 table); an invalidation level that wakes the model replaces it.
Size risky bets so that a bad month cannot take the account near the 50% drawdown halt (portfolio.to_halt_pct; B9
gives the stop odds by sig_d): the halt freezes the bot for the rest of the year. The 84-day trend state (B10) and
the gold sleeve (B11) are the two measured ways a one-year book beat USDT in more than half of the windows; both are
data for the decision, neither is enforced by code.
