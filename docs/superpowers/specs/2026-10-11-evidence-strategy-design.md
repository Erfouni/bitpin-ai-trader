# Evidence strategy and evaluation design

## Intent and execution scope

The owner requested additional personal whale wallets, a program to organize and control the influence of the bot's many inputs, deep research on prior approaches, repeated historical comparisons including Kimi, and step-by-step implementation with parallel work. All new collection and strategy behavior stays experimental. Kimi remains the live allocator; live configuration, execution rules, accounts and credentials are outside this change.

The owner supplied a cumulative USD 10 maximum for fresh Kimi evaluation, conditional on producing a useful answer. Treat this as a ceiling, never a spending target or a promise of improved accuracy. First make the free data/scoring pipeline work. Repository instructions prohibit this assistant from reading or using the Kimi key: prepare and test an isolated owner-run experiment instead. Recorded replies can be scored without fresh model calls. Never substitute simulated replies for measured Kimi results.

## Architecture and alternatives

Recommended: immutable receipt archive -> typed evidence with time/quality gates -> separately testable group forecasts -> nonnegative, shrunk influence with baseline fallback -> compact auditable packet -> Kimi's final proposed allocation -> existing deterministic validation/execution. This implementation builds the shadow evidence/forecast experiment and evaluation tooling, without connecting it to live decisions.

Alternatives are a larger raw prompt and an autonomous multi-agent trading committee. Keep the raw prompt as a paired experimental comparator; a committee adds cost and correlated narratives without an established benefit here. FinMem, FinAgent and TradingAgents inspire retrieval/structured communication, but their author backtests do not validate Kimi or Bitpin profitability.

## Evidence management

Group related economic evidence into `technical`, `flow`, `macro`, `whale`, and `news`; execution feasibility remains a separate role, not a bullish vote. An evidence record carries identity, source, market/asset, units, event time, receipt/availability time, maximum age, quality status, metrics and provenance/hash. Duplicate records and several transforms of one event cannot create extra independent votes. Select records using availability at the cutoff, not merely a past event timestamp. Missing, collecting, disputed, stale, revised or invalid data contributes no predictive weight. An unknown publication timestamp is disclosed.

Probability targets must be explicit. The new experiment predicts next-day `down`, `flat` or `up` relative to a prespecified cost band; this is distinct from the existing Kimi probability of target-before-invalidation within its plan horizon. Overall allocation confidence also has multiple meanings and must never be pooled into candidate-probability calibration.

Use a trained baseline distribution and group forecasts of the same event:

`p = (1 - sum(a_g)) * p_base + sum(a_g * p_g)`.

Weights are nonnegative, capped by group and sum to at most one. Unavailable groups return their influence to the baseline, without amplifying remaining groups. Caps are research governance limits, not empirically proven optimal percentages: technical 0.50, flow 0.20, macro 0.15, whale 0.05, news 0.10. Learn actual influence from past calibration errors with shrinkage; retain zero for untrained/unvalidated groups. Preserve the fitted period, label maturity, sample count, trial family and code/data hashes. No new data source is admitted merely because it is accurately measured.

## Additional wallet cohort

Use an immutable reviewed Ethereum address catalog, at most four wallets, including the existing Vb 3 Safe. Add only candidates with primary public attribution and independently checked finalized RPC balances. Report personal association separately from current ownership proof or firm-capital ambiguity. A zero-native wallet with large token holdings does not expand native-ETH coverage meaningfully. Do not scrape labels on every collection, add guessed addresses, infer independence from balance size, or call transfers purchases/sales.

Both providers must agree on mainnet, a common finalized anchor and every wallet's exact wei balance, with stable and prior-anchor checks. Keep the 20-request budget, two-second spacing, cooldowns and isolated state. Preserve existing Vitalik output fields while adding per-wallet rows; old observations lacking a new wallet remain unknown for that wallet. A roster change cannot fabricate earlier receipts or balances. The report displays each address, attribution/limitations, native balance and individual 24-hour/seven-day changes.

## Historical evaluation

Inventory actual archives first. There are only 51 recorded Kimi decisions (48 valid) and 18 full contexts in the initial server audit; newly collected flow/DeFi/wallet records do not establish years of original historical availability. Historical Bitpin candles can support a clearly labelled reconstructed price experiment. Today's revised DeFi arrays, new wallet labels, books, news and STRATEGY_KNOWLEDGE must not be inserted into earlier decisions as though they were available then.

Use synchronized, closed, consecutive candles with exact symbols/quote units and no forward fill. A daily decision consumes the latest completed bar and evaluates the following open to a later scheduled open. Store outcome labels separately from model input. Forecast variants are prespecified: baseline, trend, mean reversion and combined technical model. Fit transforms/models using past training observations only; select influence on a later past calibration window; purge labels that have not matured before the next stage. Split all symbols by calendar time. Use chronological development folds and an untouched final 180-day holdout. Record every variant, rather than repeatedly tuning to obtain an attractive test percentage.

Report confusion matrix, accuracy, class balance, Brier/log loss, majority/prior benchmarks, episode/calendar-day counts, fold/regime results, and paired moving-block bootstrap intervals with block-length sensitivity. Apply a centered bootstrap reality-check diagnostic to the prespecified candidate family. Repeated bootstrap samples are uncertainty calculations, not additional market observations. A simple long/cash simulation is a research comparator, with declared fee/slippage stress and same-currency benchmarks; it is not a replay of all live routing, ladder, fills or account constraints.

Recorded Kimi candidate p means target reached before an hourly close below invalidation within the plan horizon. Use contemporaneous plan/entry price and synchronized hourly quote-relative outcome paths. Missing bars, unresolved ordering or unfinished horizons are censored/unevaluable, never invented wins/losses. Report original model probabilities separately from new three-class forecasts. The geometric p0 is a benchmark, not a calibrated historical base rate.

## Kimi architecture experiment

Prepare paired raw-history and compact-evidence prompts on the same eligible episodes. Keep dates/entities blinded in retrospective prompts where possible; this reduces an identification channel but cannot remove knowledge in a later-trained model. Disable tools/web and never feed future labels or today's learned strategy corpus. Validate strict structured replies and bind them to input hashes. Cache successful replies for free rescoring; failures and repeated model calls do not create independent samples. Preserve model, prompt, code/data versions, usage, cost reservations and timing.

The owner-run runner reserves conservative worst-case input/output dollars before each call in an isolated cumulative ledger, maximum USD 10 across resumes. Reserve failed/unknown-billing calls, never refund a timeout automatically, never silently retry, and stop on malformed/truncated replies. A small paid pilot can measure feasibility and disagreement, but cannot establish durable accuracy or profitability. Fresh responses remain `not_run` until actually obtained.

## Prospective record and acceptance

Archive original public snapshots prospectively with first archive-observation time; do not backdate copied files. A separate shadow report exposes source coverage, influence, incomplete history, test provenance and promotion status. A strategy is `research_only` or `inconclusive` until temporal integrity, adequate held-out calendar coverage, calibration and after-cost comparisons justify a separate promotion review. No arbitrary accuracy target or repeated test-until-win loop is permitted.

Stdlib Python 3.7–3.14, server 3.12. No secrets, authenticated Bitpin calls, orders, account-state writes, live settings edits, main-bot restart or changes to other services. Use only separate shadow paths/services. Test meaningful failure modes and run full compatibility suites. Inspect actual desktop/mobile reports. A fresh reviewer must inspect the final branch before completion.
