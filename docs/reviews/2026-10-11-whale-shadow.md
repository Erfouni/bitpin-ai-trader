# Bitcoin wallet shadow review

Scope: a separate, free public BTC custody-address observation pilot. No live context integration, account calls, model calls or order operations were added.

An independent reviewer examined amount parsing, source agreement, bounded pagination, reorganization handling, freshness, failure suppression, state isolation and the report. Three material findings were reproduced and fixed with failing-then-passing regression tests:

1. Interrupted runs could leave a previous successful snapshot usable. Collection now persists and publishes `collecting`/unknown before the first request. A successful-then-interrupted run and subsequent report-only output remain unknown. The systemd timeout is 15 minutes.
2. A malformed late anchor response could leave the responsible provider marked healthy without a cooldown. Such failures now mark that provider unhealthy and persist a cooldown; 429 delays are preserved.
3. JSON exponent overflow bypassed literal NaN/Infinity rejection. All nested floats are now checked for finiteness, including positive and negative overflow.

The reviewer independently retested these cases and approved isolated shadow deployment. Forty-two targeted tests pass on Python 3.7 and 3.14, with one Windows symlink skip. Full suites pass on 3.7 and 3.14: 1,916 tests, 23 platform skips. The server's Python 3.12 suite also passes: 1,916 tests, one skip.

A real local public-data collection cross-checked both providers, with three agreeing balances and 146 deduplicated eligible transactions in the bounded sample; three recent transactions were excluded by the six-confirmation filter. Fifteen sampled movements exceeded the exploratory 500 BTC threshold. These counts include historical events and are not daily flow totals.

The actual offline report was visually inspected on desktop and at a 390-pixel mobile viewport. The page fits the mobile viewport; the transaction table scrolls within its own region. Exact BTC decimals, unknown reference values, historical-attribution warnings and shadow status were visible.

Deployment exposed long automatic IPv6 fallback delays. Separate read-only probes found IPv4 responses below one second and unavailable IPv6 connections; the default Python mempool request took 26.31 seconds despite a 12-second per-socket timeout. Following official Python handler documentation and CPython source, the collector received a per-connection IPv4 socket factory. It retains stock TLS verification and original-hostname SNI; it does not patch the process-global resolver or change any host networking. The revised server tip probes completed in 0.75 and 0.27 seconds. Tests cover IPv4-only resolution, hostname/certificate verification, and closure of failed sockets. A focused independent re-review approved this adapter and additionally checked multi-address fallback and source binding on both supported test interpreters.

The installed hk collector completed successfully at 2026-10-11 15:07:55 UTC (18:37:55 Tehran), after 141.47 seconds and 40 public requests. Both explorers reported status `ok` at height 970946, with three agreeing balances, 148 eligible sampled transactions and 15 exploratory large movements; one recent transaction was excluded. The snapshot explicitly reports `live_enabled=false`, `orders_created=0`, `model_calls=0` and `complete_daily_flow=false`. Daily references remain unknown until sufficient observed history exists. The enabled hourly timer is waiting for its next run at 19:37:55 Tehran. The main bot remains active with unchanged PID 27194 and its original 17:33:28 Tehran start time; the public-flow service and DefiLlama timer remain active.

Attribution remains the principal research limitation: a 2022 self-published custody-address list is not current proof of control or a complete list of private investor whales. No predictive trading benefit has been established. Daily references require future collection; live activation remains a separate owner decision.
