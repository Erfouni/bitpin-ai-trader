# Bitcoin Wallet Shadow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install a free, public BTC custody-address observation pilot without affecting trades.

**Architecture:** A separate GET-only collector cross-checks two explorers, stores time-stamped observations, and produces JSON and offline HTML. A separate hourly timer runs under the existing public-data user with no access to live state.

**Tech Stack:** Python stdlib, SQLite, unittest, systemd, offline HTML/CSS.

**Spec:** `docs/superpowers/specs/2026-10-11-whale-shadow.md`

## Global Constraints

- Python 3.7–3.14; stdlib only; server Python 3.12.
- No keys, account calls, orders, model calls, live settings changes, or notifications.
- Exactly three pinned historical published BTC addresses; attribution remains unverified today.
- Two pages/address/provider, 64 requests/run, two-second spacing, provider cooldowns.
- 500 BTC exploratory threshold, six confirmations for transaction display; balance snapshots are provisional chain balances.
- Unknown reference history or provider disagreement must not become zero or fresh data.

## Review Focus

- Mixed-input transactions and unknown change addresses must not be attributed to independent whales.
- A chain change or moving pagination must invalidate usable metrics.
- A failed provider or persisted cooldown must not block recording the other provider's status.
- Stale report-only output must hide balance changes and transaction metrics.
- Directory aliases/symlinks and malformed integers must be rejected before writes or use.

### Task 1: Public observations and daily history

**Files:** `bitpin/whale_public.py`, `bitpin/whale_shadow.py`, `tests/test_whale_shadow.py`.

**Interfaces:** `address_stats(payload, address)`, `normalize_tx(payload)`, `classify_tx(tx, addresses)`, `WhaleClient.get(provider, path)`, `WhaleStore.save(record)`, `WhaleStore.snapshot(now)`, `WhaleCollector.collect()`.

- [x] Write tests for integer parsing, internal/ambiguous movements, normalization, deduplication, provider disagreement, pagination movement, chain anchors, cooldowns, and 24-hour/seven-day missing history.
- [x] Run `py -3.14 -B -m unittest discover -s tests -p test_whale_shadow.py`; observe missing-feature failures.
- [x] Implement bounded transport, cross-checking, isolated persistence and snapshot semantics.
- [x] Run the targeted tests and full project suites on 3.7 and 3.14.

### Task 2: Report, command and service

**Files:** `bitpin/whale_report.py`, `scripts/whale_shadow.py`, `deploy/bitpin-whale-shadow.service`, `deploy/bitpin-whale-shadow.timer`, `docs/WHALE_SHADOW.md`, `tests/test_whale_cli.py`.

- [x] Test offline freshness, forbidden state directories, CLI failure exit and report escaping.
- [x] Observe RED, implement CLI and report, run tests.
- [x] Render real collected JSON on desktop/mobile and inspect the report.
- [x] Obtain an independent whole-branch review; fix material findings with regression tests.
- [x] Install the isolated collector on hk, run server tests, and verify source agreement, timer/state and the unchanged live-bot process.

Release branch: `codex/whale-wallet-shadow`, kept separate from the main branch.

## Execution rulings

Native implementation follows the owner's explicit request to add a qualifying source experimentally and standing delegation of technical design. Reuse the existing isolated checkout on a new codex branch. No additional approval is required for this authorized shadow extension; live activation remains a separate owner decision.

The transaction history is deliberately a bounded sample. This avoids claiming exact daily flow coverage from incomplete pagination or misleading on-chain address attribution. Daily features are observed stock changes, not transaction sums.

The referenced legacy HANDOFF.md is absent in both available checkouts. Current runtime state and repository instructions were verified from the preceding deployment audit and available CLAUDE.md.
