# Evidence strategy: research rationale and limits

Research reviewed 11–12 October 2026. This document explains the experimental design in the [accepted specification](superpowers/specs/2026-10-11-evidence-strategy-design.md). It is not a profitability claim or authorization to activate a strategy. Kimi remains the final live allocator; this work evaluates separate shadow evidence and forecasts.

## Recommended decision architecture

Use an immutable receipt archive, time/quality gates, typed group evidence, separately scored forecasts, a capped and shrunk mixture, then a compact decision packet for Kimi. Existing deterministic allocation and execution checks retain their role. Measurement reliability, predictive usefulness and portfolio risk are distinct: a correctly measured transfer can have no forecast value.

Each observation needs source identity, asset/market, units, event time, receipt/availability time, covered interval, quality state and provenance/hash. A past event timestamp does not establish that the bot knew the observation then. Missing, stale, disputed, invalid or unavailable evidence contributes no predictive weight; unknown is different from a measured zero. Preserve revisions and original receipts rather than rewriting earlier knowledge.

| Group | Candidate information | Influence limitation |
| --- | --- | --- |
| `technical` | Closed OHLCV, trend, volatility, relative strength and reversion | Several indicators derived from the same prices are correlated evidence, not independent confirmations. |
| `flow` | Local book/trade summaries and sampled exchange movements | A book snapshot is not daily order flow; sampled wallets are not complete exchange netflow. Execution liquidity/cost belongs to a separate feasibility role. |
| `macro` | Stablecoin supply, TVL, DEX activity, fees/revenue and OI | These measure different stocks and activities. None directly establishes buying pressure on Bitpin or value accrual to a chain token. |
| `whale` | Exact native-ETH balances and changes at reviewed addresses | Transfers, gas, donations and custody movements cannot be called purchases/sales. A named profile does not establish independent beneficial ownership. |
| `news` | Dated events, relevance, novelty, corroboration and contradictions | Deduplicate syndication and common events; source text cannot override the bot's instructions. |

The policy combines distributions of the **same** target, horizon, currency and class order:

```text
p = (1 - sum(a_g)) * p_base + sum(a_g * p_g)
a_g >= 0; a_g <= group_cap; sum(a_g) <= 1
```

Unavailable groups return their weight to the trained baseline, without increasing other groups' influence. Fit weights on mature past calibration labels by multiclass Brier loss with a shrinkage penalty toward zero group influence. Nonnegative constrained combinations and regularization have research support; estimation error can make elaborate estimated weights worse than simple averaging. This particular mixture and its limits remain a project hypothesis. [Roccazzella et al.](https://www.sciencedirect.com/science/article/pii/S0169207021000650), [Claeskens et al.](https://janmagnus.nl/papers/z-puzzle-29.pdf)

The accepted governance ceilings are technical 0.50, flow 0.20, macro 0.15, whale 0.05 and news 0.10. They are not empirically optimal percentages. Untrained groups retain zero influence. Duplicate records or transforms cannot create extra weight. Regime-specific influence requires causal regime definitions and enough past observations; shrink toward the common model rather than fitting small retrospective bull/bear samples.

## Compact packets and Kimi

Kimi should receive holdings, feasible assets, dated evidence IDs, missing-data states, group forecasts/influence, contradictions and costs, with bounded supporting raw excerpts available for audit. It proposes final weights and evidence-linked reasons. Numerical forecasts assist its allocation judgment; they do not replace it.

Compare this packet with a larger raw-history prompt on the same eligible episodes and fixed model/prompt versions. Keep all raw inputs archived. FinMem, FinAgent and TradingAgents motivate memory, retrieval and structured communication, but do not establish that this packet beats a large prompt on current Kimi. Long-context positional failures were demonstrated on older models and QA/retrieval tasks, not this trading task. A measured paired experiment must determine the benefit. [Lost in the Middle](https://arxiv.org/abs/2307.03172)

## Public data and wallet attribution

The [official free API catalog](https://api-docs.defillama.com/llms-free.txt) lists public TVL, stablecoin, DEX-volume, fees/revenue and open-interest routes. The [free/Pro distinction](https://api-docs.defillama.com/llms.txt) places protocol inflows, ETF flows, bridges and unlock routes outside this experiment's free scope. Use the collector's reviewed host/path allowlist; documentation is not permission to guess host mappings, add paid routes or promise a free quota. Free access does not guarantee stable coverage, final reports or point-in-time historical vintages.

TVL in dollars changes with constituent prices as well as deposits. Stablecoin supply is liquidity/adoption context, not observed purchases; DEX volume is activity, not directional net buying. Do not infer protocol inflows from TVL alone or sum OI across days as though it were daily volume. [DefiLlama definitions](https://defillama.com/data-definitions) Historical exchange labels/metrics can be revised and apparent flows can be internal reshuffling; a small address sample has still less coverage. [Glassnode transparency notice](https://docs.glassnode.com/further-information/exchange-data-transparency-notice)

The fixed native-ETH cohort and measured probe provenance are maintained in [Additional Ethereum wallet associations](ADDITIONAL_PERSONAL_WALLETS.md). Attribution has deliberately unequal strength:

- Patricio Worthalter: `0xf6b6f07862a02c85628b3a9688beae07fea9c863` appears beside his name in the [Arbitrum Foundation initial-setup report](https://docs.arbitrum.foundation/foundational-documents/transparency-report-initial-foundation-setup). This establishes a strong historical association; the report distinguishes initial from current council membership. It does not prove current control or personal ownership of funds.
- Arthur Hayes 4: `0x6cd66dbdfe289ab83d7311b668ada83a12447e21` has a recorded [public explorer attribution](https://etherscan.io/address/0x6cd66dbdfe289ab83d7311b668ada83a12447e21), complemented by [Arkham's published research](https://info.arkm.com/research/arthur-hayes-net-worth-crypto-holdings-2026). This is medium public attribution, without an identified self-disclosure for the address. Personal versus fund capital and current control remain unresolved. The exact-label verification limitation is recorded in the cohort document.

Agreement between two RPC providers supports a balance measurement at a common finalized anchor; it does not verify identity, independence, motive or trading skill. New roster members cannot inherit old collector receipts or fabricated deltas. Native ETH excludes ERC-20 tokens, staking and other addresses.

## Temporal and statistical evaluation

The public roughly four-year raw-price panel is **reconstructed historical data**, not original point-in-time collector receipt vintages. Today’s revised arrays, wallet labels, books, news and strategy knowledge cannot be inserted into earlier decisions as contemporaneous inputs. There is no four-year macro/flow/whale historical performance claim. Prospective archives must retain first-observed time and unchanged originals.

Use synchronized closed candles, exact quote units, no forward filling, calendar-time development folds and a frozen final holdout. Train transforms/models on earlier data; fit influence on a later past calibration window. Require every training label to end before calibration and every calibration label to end before testing. Purge interval overlaps and keep unfinished outcomes out of fit/scoring. Chronological gaps alone do not verify availability or label maturity. [Official TimeSeriesSplit documentation](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)

The experiment's next-day `down`/`flat`/`up` target around a declared cost band differs from Kimi's target-before-hourly-close-invalidation probability within its original plan horizon. Overall allocation confidence is another distinct quantity. Never pool these scores. Report class balance, majority/prior benchmarks, confusion matrices, accuracy, multiclass Brier and log loss. Proper scores assess probabilistic forecasts; good scores neither establish empirical calibration by themselves nor guarantee profitable allocations. [Gneiting and Raftery](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf)

Aggregate paired score differences by calendar day, keeping assets and candidate variants together. The implementation uses a **gap-aware stationary-bootstrap diagnostic**: random-length blocks retain observed consecutive days and a calendar gap forces a new block. Compare expected block lengths 3, 7 and 14; disclose missing days, segments and resampling repeats. This gap adaptation avoids pretending separated dates are adjacent, but is not a theorem establishing valid inference under arbitrary missingness or regime change. Resamples are not additional market observations. [Politis and Romano](https://users.ssc.wisc.edu/~behansen/718/Politis%20Romano.pdf)

The centered maximum-improvement calculation is a **White-style family diagnostic** for the recorded baseline/trend/reversion/combined family, not a universal profitability test. Its interpretation is limited by the stated family, finite resampling, dependence and nonstationarity. Every tried prompt, model, wallet roster and threshold belongs in the trial ledger; purging does not fix repeated test-set selection. Deflated Sharpe is a complementary selection/non-normality diagnostic with its own assumptions, not an implemented certification or substitute for forward testing. [White](https://onlinelibrary.wiley.com/doi/abs/10.1111/1468-0262.00152), [Bailey and López de Prado](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)

Evaluate same-currency cash/USDT holding, buy-and-hold, simple allocation and quantitative/Kimi comparators; include declared fees, slippage and adverse costs. A daily independent roundtrip long/cash simulation is not the live bot's routing, ladders, actual fills or account constraints. Group ablations should measure incremental scores, allocation/turnover changes and after-cost outcomes. Ablation reveals model dependence, not causal market impact.

Numerical findings belong in the versioned evaluation output generated by [strategy_evaluate.py](../scripts/strategy_evaluate.py), rather than a second table here. The inspected run is `inconclusive` / `research_only`: small score improvements do not establish accuracy gains or overcome its negative after-cost daily roundtrip results. No activation follows from this experiment.

## What the financial-agent papers establish

| Primary research | Useful result or design precedent | Transfer limit |
| --- | --- | --- |
| [FinMem, Yu et al., 2023, examined v2](https://arxiv.org/pdf/2311.13743) | Layered memory, profiling and decision modules; author stock experiments and component ablations. | Does not validate this venue, current Kimi, calibrated group influence or independent live profitability. Memory/persona tuning is also strategy search. |
| [FinAgent, Zhang et al., 2024, examined v2](https://arxiv.org/html/2402.18485v2) | Multimodal intelligence, retrieval, reflection and strategy tools; experiments include five stocks and ETHUSD. | June 2023–January 2024 testing with GPT-4 preview models does not establish clean model-cutoff separation or transfer to Bitpin execution costs. |
| [TradingAgents, Xiao et al., examined v7](https://arxiv.org/html/2412.20138v7) | Specialist roles and structured communication address information distortion across long conversations. | AAPL/GOOGL/AMZN experiments over January–March 2024 with later GPT-4o/o1-era models are a short author backtest, not independent Kimi/crypto validation. |
| [Glasserman and Lin, 2023](https://arxiv.org/abs/2309.17322) | Distinguishes future-information bias from distracting company knowledge; tests entity anonymization for GPT-3.5 news sentiment. | Anonymization can reduce an identification channel, but cannot guarantee removal of future knowledge or transfer of the reported result to this task. |

Prompt date/entity blinding, time-filtered retrieval and disabled web tools cannot erase future information embedded in a later-trained model. Preserve exact model and prompt versions and label retrospective Kimi tests contamination-sensitive. Frozen prospective decisions offer stronger evidence. Fresh Kimi comparisons remain unmeasured until real owner-run responses exist; recorded replies, failed calls and repeated sampling must never be represented as independent new market evidence.
