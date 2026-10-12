# Evidence Strategy Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for coordination and inline integration; delegate independent bounded components in parallel as explicitly requested by the owner. Use test-driven-development and obtain a fresh final whole-branch review.

**Goal:** Expand the personal-wallet shadow cohort and build auditable evidence influence, historical scoring and a bounded owner-run Kimi architecture experiment.

**Architecture:** Separate public collectors and receipt archives feed time-gated group observations. Historical quantitative and recorded-Kimi evaluators produce frozen results; a compact packet makes group influence and missing data inspectable without changing the live bot.

**Tech Stack:** Python stdlib/SQLite/unittest, existing public RPC and candle endpoints, offline responsive HTML/CSS/JavaScript, isolated systemd units.

**Spec:** `docs/superpowers/specs/2026-10-11-evidence-strategy-design.md`

## Global constraints and rulings

- Python 3.7–3.14; server 3.12; no dependencies added to live runtime.
- No credentials, authenticated Bitpin traffic, orders, live settings or execution changes. Owner executes any credential-using Kimi experiment. Cumulative fresh-call budget USD 10, including failures/resumes; no speculative paid exploration.
- Experimental source caps: technical .50, flow .20, macro .15, whale .05, news .10; absent/untrained groups return influence to baseline. These are governance limits, not claimed optimal weights.
- Wallet catalog <=4; collection <=20 calls, 2 seconds between, provider cooldown >=1 hour, at most 1 MiB/RPC response. Native ETH only, current ownership unverified.
- Historical forecasts use only mature past labels and time-eligible records. No forward-filled candles, future label inputs, retrospective use of today's knowledge/news/labels, or fabricated old collector receipts.
- Branch `codex/evidence-strategy-shadow` based on `59396f2`. HANDOFF.md is absent from both available checkouts; record the limitation rather than invent its contents.
- Ruling: the owner explicitly requested plan plus step-by-step implementation and parallel execution, and delegated technical choices. Proceed within this shadow scope without another design-permission round. Fees are a declared research assumption; no actual account fee endpoint will be called.

## Review focus

1. Same-time records with future first-observed timestamps must be excluded even if event timestamps are old.
2. Mixed probability targets, units, symbols or overlapping labels must not inflate accuracy or calibration counts.
3. Lost model responses and concurrent/resumed experiments must not reset dollar reservations or bypass the ceiling.
4. Roster migration must leave new-wallet historical deltas unknown while retaining Vitalik compatibility.
5. Missing/illiquid bars and unfinished outcomes must remain excluded, with explicit denominators and no hidden forward filling.

## Task 1: Provenance inventory and research

**Files:** create `docs/EVIDENCE_STRATEGY_RESEARCH.md`; private raw data/results remain ignored under `scratch/`.

- [x] Audit local data and known non-secret server logs/public state; count historical coverage and probability semantics.
- [x] Research primary financial-agent, proper-scoring, forecast-combination, temporal-validation and multiple-testing sources in parallel.
- [x] Finish additional-wallet primary attribution and fixed-block two-provider checks.
- [x] Save the rationale, exclusions, source links and point-in-time gaps.

## Task 2: Typed evidence, influence and statistics

**Files:** create `bitpin/evidence_policy.py`, `bitpin/evidence_statistics.py`, `tests/test_evidence_policy.py`, `tests/test_evidence_statistics.py`.

**Interfaces:** `normalize_probs(values) -> list[float]`; `mixture(base, forecasts, weights, caps) -> dict`; `fit_influences(rows, groups, caps) -> dict`; `score_predictions(rows) -> dict`; `paired_block_bootstrap(daily_differences, block_size=7, repeats=500, seed=...) -> dict`; `reality_check(daily_candidate_differences, block_size=7, repeats=500, seed=...) -> dict`; `assemble_packet(cutoff, symbol, observations, baseline, forecasts, influence) -> dict`.

- [x] Write failing tests for unknown/future/stale/duplicate evidence, target mismatch, invalid probabilities and baseline reversion.
- [x] Implement strict time/quality gates and bounded group mixture, with hash/provenance and no network/account imports.
- [x] Write failing tests for exact score examples, paired calendar blocks, empty/small samples and mature calibration labels.
- [x] Implement shrunk nonnegative influence fitting, scoring, bootstrap uncertainty and family diagnostic; verify on independent hand calculations.

## Task 3: Additional wallet cohort (parallel)

**Files:** create `bitpin/personal_wallets.py`; modify existing `personal_whale.py`, `personal_whale_report.py`, personal-wallet tests/docs only.

**Interfaces:** immutable `WALLETS` catalog; existing CLI and Vitalik scalar fields retained; snapshot adds `wallets` rows with exact native ETH and per-wallet reference values.

- [x] Add failing tests for allowlisted multiple addresses, fixed common anchor, per-wallet disagreement and unknown pre-introduction references.
- [x] Add reviewed qualifying candidates and migrate old records without inventing balances or receipts.
- [x] Render per-wallet attribution and ambiguity, exact quantities and actual intervals.
- [x] Verify focused tests, rendered desktop/mobile report, server staged compatibility, installed hashes and successful isolated collection.

## Task 4: Historical data and quantitative/recorded-Kimi scoring (parallel)

**Files:** create `bitpin/strategy_data.py`, `bitpin/strategy_walkforward.py`, `bitpin/kimi_score.py`, `scripts/strategy_evaluate.py`, associated tests.

**Interfaces:** `build_episodes(panel, horizon=86400, warmup=60) -> list[dict]`; episodes have `episode_id`, `decision_time`, `label_end`, `symbol`, `features`, blinded normalized `history`, separate `label`/`realized_return` and provenance. `evaluate_walkforward(episodes, ...) -> dict`; `score_recorded_candidates(decisions, bars, ...) -> dict`.

- [x] Add tests for closed candles, exact adjacency, synchronized quote-relative data, current-bar exclusion, missing outcomes and input/label separation.
- [x] Import/download a bounded public daily price sample with receipts/hashes and explicitly reconstructed-history provenance.
- [x] Implement prespecified baseline/trend/reversion/combined models; mature-label chronological train/calibration/development folds and final 180-day holdout.
- [x] Report confusion/proper scores, calendar counts, cost-stressed long/cash comparators, paired intervals and every tried variant.
- [x] Score sanitized actual Kimi candidates against matching hourly outcomes; separate confidence definitions and censored cases. Do not claim that current forecasts used newly absent historical factors.

## Task 5: Owner-run Kimi architecture experiment (parallel after episode interface)

**Files:** create `bitpin/kimi_experiment.py`, `scripts/kimi_strategy_experiment.py`, associated tests and `docs/KIMI_STRATEGY_EXPERIMENT.md`.

**Interfaces:** prepare blinded paired raw/compact messages from eligible episodes with labels removed; validate structured probability/weight replies; persist input-bound cached replies and a locked cumulative reservation ledger. Read credentials only inside the explicitly owner-run execution path, never offline prepare/score.

- [x] Write failing tests for labels/identities excluded from prompts, response schema, cache binding, unknown billing, retries disabled and cumulative/concurrent budget safety.
- [x] Implement dry-run/prepare and offline scoring without credentials or network. Enforce a <=10 USD ceiling with conservative K3 pricing and output limits.
- [x] Prepare a small predeclared paired experiment only after free data/scoring succeeds; leave fresh results `not_run` until actual responses exist.
- [x] Provide exact owner-run commands, estimated worst-case cost and useful-result limitations; do not ask for a key.

## Task 6: Prospective archive and research report

**Files:** create `bitpin/strategy_shadow.py`, `bitpin/strategy_report.py`, `scripts/strategy_shadow.py`, service/timer units and tests.

- [x] Write failing tests for safe separate state, atomic artifacts, first-archive availability, changed/error snapshots and no network/model/order calls.
- [x] Archive only fixed public collector snapshots prospectively; construct the compact packet with trained/experimental influence and visible missing data.
- [x] Build a polished source-backed report with fold/variant comparisons, confidence intervals, wallet coverage and explicit promotion status.
- [x] Inspect actual desktop and mobile rendering; install only the separate public shadow archive after staged checks.

## Task 7: Integration, review and delivery

- [x] Run full suites on Python 3.7, 3.14 and server 3.12 after all changes.
- [x] Obtain a fresh whole-branch review, reproduce and repair material findings, then rerun affected/full checks as needed.
- [x] Verify deployed collector/archive hashes, timer/result state and unchanged live bot/other services.
- [x] Save reproducible raw inputs privately and aggregate findings/research in docs; commit the feature branch locally.
- [x] Deliver measured results with denominators, intervals and limitations. Distinguish implemented, tested, installed, fresh-Kimi-not-run and statistically-inconclusive work.

- [ ] Owner chooses Git integration: merge into the known base, push/create PR, or retain the branch. No integration is automatic.

Fresh paid Kimi responses and predictive promotion are explicitly pending empirical work, not completed implementation claims. See `docs/dev_notes/EVIDENCE_STRATEGY_DELIVERY.md` for final findings, verification, rulings and limitations.
