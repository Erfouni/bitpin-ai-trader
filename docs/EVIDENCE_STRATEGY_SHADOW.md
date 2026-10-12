# Prospective evidence strategy

This isolated experiment adds no live trading or Kimi calls. It archives the four
fixed public collector JSON files hourly, with immutable full SHA-256 snapshots,
compressed payloads and an independent first archive observation time. Collector
event and receipt dates never replace that first-observed date. No old macro,
flow, news or whale snapshots are invented for historical tests.

The collector accepts daily trade-pressure coverage only; short windows are
archived for provenance. TVL, DEX volume, stablecoin supply, fees, revenue and
open interest remain distinct observations within one macro group. Native wallet
balances remain contextual evidence, with no assertion of buying or selling.
All new groups have zero predictive influence until properly calibrated.

`evidence_policy.py` gates quality, timing, freshness and matched probability
targets. Nonnegative influence fits minimize Brier error with L2 shrinkage under
group ceilings; missing forecasts return their allocation to the baseline. The
ceilings (50/20/15/5/10 percent for technical/flow/macro/whale/news) are experimental
governance choices, not optimal trading weights. Execution depth is separate
from predictive influence. Kimi remains the live final allocator.

## Local checks

```text
python -m unittest tests.test_evidence_policy tests.test_evidence_statistics tests.test_strategy_shadow tests.test_strategy_report
python scripts/strategy_shadow.py --state-dir state/strategy-shadow
```

On Windows the fixed Linux public input paths are unavailable; the resulting
report displays missing input coverage. Use explicit in-memory fixtures for
tests. The CLI cannot be pointed at arbitrary credential/config paths.

## Isolated server installation

Stage and test the branch separately before installation. Copy its `bitpin/`,
`scripts/`, `docs/evidence-strategy-results.json` and font assets into
`/opt/bitpin-strategy-shadow`. Install the two matching deploy units, then run:

```sh
sudo systemctl daemon-reload
sudo systemctl start bitpin-strategy-shadow.service
sudo systemctl enable --now bitpin-strategy-shadow.timer
sudo systemctl show bitpin-strategy-shadow.service -p Result -p ExecMainStatus
```

This service has no network and cannot read live settings, account state, home
directories or Xray. It can read only public collector files and writes its own
marked state at `/var/lib/bitpin-strategy-shadow`. The source collectors and live
bot do not need a restart. Artifacts are `archive.sqlite3`, `strategy-shadow.json`
and `report.html`. A process lock prevents concurrent archive writes; database
capacity is 512 MiB and fills fail clearly rather than deleting prior receipts.

The current comparison and conclusions are in
[STRATEGY_EVALUATION.md](STRATEGY_EVALUATION.md). Fresh model research is separate:
[KIMI_STRATEGY_EXPERIMENT.md](KIMI_STRATEGY_EXPERIMENT.md). No future promotion is
automatic; neither a small accuracy percentage nor a bootstrap p-value establishes
profitable decisions.

## Installed shadow verification — 2026-10-12

The tested source was installed on `hk` only in the two separate public research
applications. The personal collector returned `ok` for three addresses in 16
requests, with both providers agreeing at finalized mainnet block 26174016,
hash `0xb19e885041537ba7a61194eb35360f99acdf496867d481105f4d913a2361ed78`.
The two new wallet histories have unknown 24-hour/seven-day changes; no old
balances were invented. Each application's `deployment-manifest.json` hashes
the installed allowlisted source/assets. Both services exited successfully and
their hourly timers are enabled and active.

The archive captured all four public sources. Macro coverage was eligible,
wallet coverage eligible for BTC/ETH, and daily flow coverage still warming up
with less than 86,400 seconds of uninterrupted coverage. Every prospective group
weight is zero; packet hashes were independently recomputed. The service's
actual systemd properties confirm its private network, AF_UNIX restriction,
public read-only paths and sole writable research state. Live bot PID 27194
and public-flow PID 8845, including their start times, were unchanged.

All 2,034 tests pass on Python 3.7 and 3.14 (24 platform skips each) and the
server's Python 3.12.3 (two platform skips). A fresh whole-branch review found
four material issues; regression tests reproduced and fixed shared-budget
resets, full-hour target/invalidation ordering, falsely relabelled USDT panels,
and mismatched forecast identity/timing. Actual deployed reports were inspected
on desktop and at a 390-pixel mobile viewport, with embedded Vazirmatn loaded
and no page overflow. No live promotion or fresh model call occurred.
