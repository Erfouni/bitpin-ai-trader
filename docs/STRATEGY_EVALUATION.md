# Historical strategy evaluation

This is a reconstructed public-price experiment and an offline scorer for
recorded Kimi candidate probabilities. It does not execute orders, access an
account, read credentials, call a model, or alter the live bot. Promotion remains
`research_only`; the measured result is `inconclusive`.

## Data and frozen targets

The public chart endpoint observed in this project is
`https://api.bitpin.org/v1/mkt/tv/get_bars/?symbol=BTC_IRT&res=1D&from=<seconds>&to=<seconds>`.
The owner collected bounded responses for BTC_IRT, ETH_IRT, SOL_IRT and USDT_IRT.
The raw JSON, receipt URLs/times and SHA-256 inputs remain ignored in `scratch/`.
Historical revisions are possible. A receipt obtained now establishes when this
reconstruction was obtained; it cannot establish original point-in-time news,
order books, macro data, wallet attribution or collector availability.

`build_episodes` accepts a symbol-to-OHLCV mapping, optionally wrapping each
series in `{bars, receipt}`. Timestamps are candle opens. Prices must be finite,
positive and inside coherent OHLC bounds; volume must be positive. Duplicate
timestamps, malformed records and forming bars are excluded. Every 60-bar input
and the complete outcome interval must be exactly consecutive. An excluded bar
creates a gap; there is no forward fill.

A decision consumes 60 completed daily bars and enters at the next scheduled
open. The outcome is the open exactly 24 hours later. Labels are `0=down`,
`1=flat`, `2=up`, using a fixed fractional band of 0.008, with both boundaries
inside flat. This band declares two taker fees of 0.0035 and slippage of 0.0005
per side; it is a research assumption, not an observed account fee.

IRT and USDT returns are separate targets and reports. USDT returns divide
synchronized coin and USDT_IRT open/close prices. Independent OHLC extrema
cannot identify simultaneous ratio extrema, so normalized experimental history
contains only open, close, relative step and normalized base volume. History
has no asset identity, timestamps or outcome. Features are fractional 1/7/30-day
close returns, population standard deviation of 14 daily returns, and the last
volume divided by its 30-day mean minus one. `input_hash` binds history/features;
outcomes remain separate episode fields.

## Prespecified models and temporal evaluation

The complete trial family is baseline, trend, mean reversion and combined.
Baseline uses Laplace-smoothed training class frequencies. Other models use
training-only standardized features and three diagonal Gaussian class models.
Class moments shrink toward overall training moments by 20 pseudo-observations;
standardized variance has a 0.2 floor. Feature log likelihoods are averaged to
reduce the effect of correlated transforms. These are declared regularization
choices, not parameters optimized on held-out results.

Trend uses 7/30-day return and 14-day volatility; reversion uses 1/7-day return
and relative volume; combined uses all five features. Each model's probability
forecast is mixed with baseline using nonnegative L2-penalized Brier fitting on
the immediately preceding calibration interval. Technical influence is capped
at 0.50. No historical macro, flow, news or whale influence is fabricated.

All symbols split at the same calendar timestamp. Defaults are at least 180
initial training days, 90 past calibration days, development folds of 90 days,
and an untouched final 180-day holdout. Training labels must end strictly before
calibration begins; calibration labels must end strictly before testing begins.
Each candidate and its weight are recorded. Selection minimizes calibration
Brier only, including the final frozen choice immediately before holdout.

Reports include count, class counts, confusion matrices, accuracy, Brier, log
loss, training-majority accuracy, fold results and regimes defined by past
30-day return. Correlated assets are averaged within a calendar day before
paired Brier differences are resampled. A gap-aware stationary bootstrap uses
500 repeats and expected block lengths of 3/7/14 days. A centered family reality
check covers all three technical candidates. Bootstrap repetitions are not extra
observations, and reported diagnostics are not a promotion decision.

The long/cash comparator independently closes/reopens each daily exposure when
up has strictly the greatest probability. Fees/slippage apply on both sides;
stress doubles their sum. It reports the same-currency daily equal-weight long
comparator with identical costs, cash, and gross results. The IRT report also
shows an exactly synchronized USDT buy/hold benchmark with endpoint costs.
Daily compulsory round trips make this deliberately simple comparator costly.
It does not replay live hold discipline, ladders, route selection or fills.

## Recorded Kimi outcomes

Candidate `p` means target reached before an hourly close below invalidation
within the original 24–720-hour plan. Contemporaneous `plans` and entry USDT
prices are required; missing plans are never inferred from prose or carried
from an earlier reply. Allocation confidence is a different quantity and is
never pooled into candidate calibration. Unexported confidence is disclosed.

Hourly coin/USDT_IRT bars must be synchronized and consecutive. The ratio's
possible target bound is coin high / quote low; its guaranteed target bound is
coin high / quote high. A crossing known only through the possible bound is
censored. A guaranteed target in a full hour precedes that hour's closing
invalidation: a close below the lower invalidation cannot itself be the target.
A straddling first/last partial hour is usable only when whole-bar bounds exclude
target contact; otherwise event timing is unknown. Completed predecision bars
cannot win a forecast. Invalidation uses the synchronized hourly close, not an
intrabar low. Neither event by a fully observed horizon is a loss; gaps and
unfinished horizons are censored. Overlapping same-symbol forecast intervals
are excluded from independent calibration counts. The geometric benchmark
`p0=loss_distance/(gain_distance+loss_distance)` is not an empirical base rate.

## Measured free run

The first frozen run used cutoff `1791734999.6648576`. The final holdout starts
2026-04-12 20:30 UTC and has 180 days / 540 BTC, ETH and SOL episodes in each
currency. There are eleven development folds. Strict validation plus 60-bar
continuity leaves 3,168 IRT episodes across 1,283 days and 2,425 USDT episodes
across 924 days; these counts must not be described as four complete years.
These measurements condition on valid, liquid and consecutive price history;
excluded periods can have different behavior.

| Holdout variant | IRT accuracy | IRT Brier | USDT accuracy | USDT Brier |
|---|---:|---:|---:|---:|
| Baseline | 39.63% | 0.660877 | 31.11% | 0.675945 |
| Trend | 39.63% | 0.659004 | 31.11% | 0.675719 |
| Mean reversion | 39.63% | 0.660348 | 31.11% | 0.673092 |
| Combined | 39.81% | 0.659604 | 31.11% | 0.674224 |

The calibration-selected variants were IRT trend and USDT mean reversion. Their
7-day block Brier-improvement intervals over baseline were respectively
0.000843–0.003290 and 0.000390–0.005439. These small proper-score improvements
did not produce an improved class decision rule: forecasts overwhelmingly
selected up. Class counts were IRT `[170,156,214]` and USDT `[171,201,168]`.
Their daily round-trip after-cost returns were -51.46% IRT and -71.24% USDT;
the IRT USDT buy/hold comparator was +67.41%. This supports no live promotion.

The resulting design decision is to keep daily evidence as context while
withholding predictive influence from unvalidated groups. The daily compulsory
re-entry rule has no justification for deployment. A future longer-hold/turnover
gate needs a separately preregistered experiment and fresh prospective outcomes;
changing it until this same holdout looks profitable would be overfitting.

The sanitized archive has 51 decisions and 44 candidate probabilities. None
is independently evaluable: 40 lack contemporaneous plans, three overlap the
first eligible BTC plan, and that first plan crosses a zero-volume USDT hourly
bar at timestamp `1790965800`. Censoring preserves this missing path rather than
inventing a loss. Fresh paid Kimi results remain `not_run`.

## Reproduction

Run Python 3.7–3.14 from the repository root. Supply each public daily file and
its receipt with repeated `--daily SYMBOL=FILE` / `--receipt SYMBOL=FILE` flags.
Use `--currency both`, `--cutoff 1791734999.6648576`,
`--output scratch/strategy-evaluation.json`, and optionally
`--episodes-output scratch/strategy-episodes-USDT.json`. For recorded scoring,
add `--recorded scratch/recorded-kimi-sanitized.json` and repeated
`--hourly SYMBOL=FILE` / `--hourly-receipt SYMBOL=FILE` flags. The command is
`python scripts/strategy_evaluate.py`; it has no download or model-call path.
Input JSON is bounded at 4 MiB per file, receipt hashes are checked, output is
atomic, and the aggregate includes input/code hashes and all tried variants.

Focused integrity verification:
`python -m unittest tests.test_strategy_data tests.test_strategy_walkforward tests.test_kimi_score`.
The global suite must also pass before branch delivery. See the evidence strategy
specification and research notes for source provenance and remaining limitations.
