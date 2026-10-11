# Personal Whale Shadow Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement inline, then obtain one fresh whole-change review.

**Goal:** Add a free, separate personal-wallet observation pilot without affecting trades.

**Architecture:** Fixed read-only Ethereum RPC calls cross-check one labelled Safe at a common finalized anchor. Reuse the existing generic observation persistence and lock helpers, with a separate snapshot, report, directory and service.

**Tech Stack:** Python stdlib, SQLite, unittest, offline HTML/CSS, systemd.

**Spec:** `docs/superpowers/specs/2026-10-11-personal-whale-shadow.md`

## Constraints and review focus

Python 3.7–3.14; 1 MiB responses, 20 requests/run, two-second spacing, minimum hourly provider cooldowns, eight-day retention, 9,000-second receipt freshness. No secrets/live integration. Check attribution drift, RPC error/null/mismatched IDs, block rollback, late provider failure, interruption after prior success, wrong chain, balance disagreement, directory aliases and stale report-only metrics.

## Task 1: Read-only observations and history

Create `bitpin/personal_whale.py` and `tests/test_personal_whale.py`. Interfaces: `quantity(value)`, `block(value, now)`, `PersonalClient.call(provider, method, params)`, `PersonalStore.snapshot(now)`, `PersonalCollector.collect(publish=None)`, `prepare_directory(value)`.

- [x] Write and run failing tests for exact wei, RPC allowlists, malformed responses, fixed-block agreement, stale heads, early/late failure, collecting sentinel, cooldowns and reorganization.
- [x] Implement the observer and isolated directory semantics; make those tests pass.
- [x] Test receipt/reference freshness and exact 24-hour/seven-day changes, including epoch boundaries.

## Task 2: CLI, report and deployment

Create `bitpin/personal_whale_report.py`, `scripts/personal_whale_shadow.py`, service/timer units and research documentation. Interfaces: `eth(value)`, `render_report(snapshot)`, CLI `main(argv=None, client=None, clock=time.time, sleeper=time.sleep)`.

- [x] Test report-only network isolation, CLI outputs, escaping, exact ETH and unknown data.
- [x] Implement and inspect the actual report on desktop and mobile.
- [x] Run full suites on Python 3.7, 3.14 and server 3.12; obtain a fresh independent review and fix material findings.
- [x] Install only the isolated pilot on hk; verify agreed real data, hourly timer, installed hashes and unchanged live bot.

## Execution rulings

The owner explicitly authorized a qualifying free source in shadow mode and delegated technical design. Proceed within that scope without another design-approval round. Keep implementation on `codex/independent-whale-shadow`, based on the existing BTC pilot. The legacy HANDOFF.md is absent; available repository instructions and current service state govern. ETH native balance is intentionally narrower than complete portfolio or transfer tracking. Published labels are disclosed as attribution rather than current ownership proof. No `eth_call` retry or access-control bypass is authorized or needed.
