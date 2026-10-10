# Flow Shadow Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this approved plan task-by-task.

**Goal:** Collect and evaluate Bitpin and DefiLlama flows without affecting live trades.
**Architecture:** Isolated CLI with a constrained public transport, SQLite observation store, deterministic summaries and Persian HTML/JSON reports. Future integration consumes these summaries only after owner approval.
**Tech Stack:** Python standard library, SQLite, HTML/CSS.
**Spec:** docs/superpowers/specs/2026-10-10-flow-shadow.md

## Global Constraints
- Python 3.7 through 3.14; standard library only.
- Shadow only; no live config/state, credentials, authenticated exchange calls or model requests.
- Public GET requests only; unknown data never becomes a zero signal.
- Reporting units remain IRT or USDT per market.

## Review Focus
- Incomplete trade buffers or collector restarts must invalidate affected window coverage.
- Duplicate/conflicting records and future timestamps must not fabricate pressure.
- Missing provider fields must not become zero liquidity; receipt time is not provider time.
- Source failures and rate limits must not block unrelated sources or fabricate freshness.
- CLI paths, redirects and extra flags must never reach live state or authenticated endpoints.

### Task 1: Public source validation and features
**Files:** bitpin/flow_public.py, bitpin/flow_features.py, tests/test_flow_features.py
**Interfaces:** PublicFlowClient.matches(symbol), orderbook(symbol), stablecoins(); normalize_trades(payload, now); book_features(payload); liquidity_features(payload).
- [x] Write tests with hand-calculated buy/sell, depth and circulation fixtures; reject malformed/negative/nonfinite/future records and unknown symbols/URLs.
- [x] Run unittest discovery for test_flow_features.py and observe missing-feature failures.
- [x] Implement constrained HTTP and deterministic normalization/features.
- [x] Run targeted tests; expect all pass.

### Task 2: Persistent coverage and collection
**Files:** bitpin/flow_shadow.py, tests/test_flow_shadow.py
**Interfaces:** ShadowStore(path), ingest_trades(symbol, payload, now), ingest_book(symbol, payload, now), fail(source, symbol, now, error), snapshot(symbols, now); ShadowCollector(store, client, clock).collect(symbols).
- [x] Write replay tests asserting literal deltas after warmup, deduplication/reopen, outage/no-overlap resets, partial errors and independent source recovery.
- [x] Run targeted tests and observe missing-feature failures.
- [x] Implement durable SQLite observations, continuity, retention, source backoff and fixed shadow-mode snapshots.
- [x] Run both new test modules; expect all pass.

### Task 3: CLI, report and experimental validation
**Files:** scripts/flow_shadow.py, bitpin/flow_report.py, docs/FLOW_SHADOW_FA.md, tests/test_flow_cli.py
**Interfaces:** main(argv=None); write_reports(snapshot, directory) writes snapshot.json and report.html atomically.
- [x] Write subprocess tests for rejected live flags/shared state, finite runs, safe report rendering and failure output.
- [x] Run targeted tests and observe failures; implement CLI and RTL report.
- [x] Run complete suites on Python 3.7 and 3.14; expect no failures.
- [x] Run bounded public-data collection in an isolated hk directory using an unprivileged sandboxed transient service; inspect report and service isolation.
- [x] Obtain a fresh whole-branch review; fix important findings with regression tests.
- [x] Commit on feature branch; report measured results and limits. No activation of real decisions.
