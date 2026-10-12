# Evidence strategy delivery — 2026-10-12

The owner requested parallel research and stepwise implementation, additional
personal-wallet observations, controlled evidence influence, causal historical
comparison and a USD 10 maximum for a useful fresh Kimi experiment. This change
implements the research pipeline and installs its public shadow collectors.
Fresh model evaluation is prepared, not executed. No new strategy is promoted.

## Measured outcome and design ruling

The price reconstruction has eleven chronological development folds and a
final 180-day holdout with 540 BTC/ETH/SOL episodes per return currency. Frozen
selected accuracy is 39.63% IRT and 31.11% USDT, identical to the corresponding
baseline. Small Brier improvements have paired seven-day block 95% intervals
of 0.000843–0.003290 and 0.000390–0.005439. All prespecified variants and counts
are retained in `evidence-strategy-results.json`.

The deliberately daily close/reopen long/cash comparator loses 51.46% in IRT
and 71.24% in USDT after the declared costs. These are research simulations,
not live account losses or a replay of actual bot fills. The resulting ruling
is to withhold unvalidated predictive influence and reject deployment of that
daily re-entry rule. A longer holding period or turnover gate requires a new
predeclared prospective experiment, rather than tuning this exposed holdout.

Historical macro/flow/news/wallet receipts do not exist for this price sample.
Reconstructed prices cannot make those factors available in earlier decisions.
Of 51 recorded Kimi decisions and 44 candidates, zero outcomes are independently
evaluable: 40 missing contemporaneous plans, three overlapping forecasts and
one remaining path with a zero-volume hourly quote bar. No Kimi accuracy is
reported. Neither extra bootstrap repetitions nor stability calls add market
observations. Later model training knowledge remains a retrospective limitation.

## Four review findings resolved

The independent whole-branch reviewer found four material issues and no other
actionable minor findings. Regression tests first reproduced each failure:

1. A second paid state directory could reset the USD 10 allowance. All owner
   directories now share one locked, durable owner grant. Global reservation
   precedes local writes and HTTP; unknown charges retain their reservation.
   Repeated request/repeat identities cannot be retried from another directory.
2. A guaranteed target in a full hourly candle with a closing invalidation was
   wrongly censored. A target above the lower invalidation necessarily precedes
   that closing invalidation. Partial hours and uncertain ratio extrema remain
   censored.
3. A panel already quoted in USDT could be divided by USDT_IRT and falsely
   labelled USDT. Conversion now requires the declared USDT_IRT quote and IRT
   asset panels, rejecting other combinations.
4. Same-target probabilities from a different symbol, currency or future
   decision could enter a packet. Identity, exact decision/label interval,
   receipt availability and fitted target/currency metadata are now required.

All four regression paths pass. No findings were deferred. The reviewer did not
audit deployment, actual UI, RPC responses or current pricing; those checks
were separately performed and recorded below. There was no extra rereview
round after the fixes; the full compatibility suites include all fixes.

## Verification and installed state

- Python 3.14: 2,034 tests, OK, 24 platform skips, 123.285 seconds.
- Python 3.7: 2,034 tests, OK, 24 platform skips, 122.296 seconds.
- Isolated server Python 3.12.3: 2,034 tests, OK, two skips, 135.357 seconds.
- The server shell wrapper initially used incorrect PowerShell escaping after
  the successful suite. A fresh Python check verified its saved complete log
  and exited zero; this was a wrapper error, not a failing test.
- The allowlisted installed runtime, fonts and units match the tested staging
  hashes. No production code changed after the final full suites.
- The updated personal collector and new strategy archive exited successfully;
  both hourly timers are enabled and active. Existing public state was retained.
- PublicNode and Flashbots agree on all three native balances at finalized
  mainnet block 26174016. New-wallet 24-hour/seven-day deltas remain unknown.
- Four public source snapshots were archived with independently recorded first
  observation. Packet SHA-256 values were independently recomputed; all new
  prospective group weights are zero. Daily flow has incomplete continuous
  24-hour coverage and remains excluded from predictive influence.
- Actual systemd settings confirm private networking and AF_UNIX only for the
  archive, inaccessible live/private paths and its sole writable research state.
- Live bot PID 27194 and public-flow PID 8845, including start times, are unchanged.
- Actual desktop/mobile wallet and strategy reports were visually inspected.
  Vazirmatn loads; the 390-pixel views have no page overflow, and currency/period
  controls show the matching measured results. Hidden-tab capture failures were
  resolved after reading Chrome's documented `Page.bringToFront` behavior.
  A reused tab's viewport mismatch was resolved using the known mobile surface;
  it did not require a product CSS change.

## Owner-run pilot and remaining empirical work

The same frozen 16-day pilot is prepared privately on `hk`, with 34 registered
calls including a stability pair. Its first pair reserves at most USD 0.384426;
the entire registered plan reserves at most USD 6.532950. Actual calls and
reservations are zero. The overall authorized ceiling remains USD 10 across
owner directories and resumes. Use one owner account/machine and preserve its
grant. The software cannot police deliberate deletion or execution under a new
machine/account, and it cannot guarantee a useful answer.

Repository `CLAUDE.md` prohibits assistant use of the Kimi key. Exact owner
commands and credential-free preparation/score paths are documented in
`KIMI_STRATEGY_EXPERIMENT.md`. No real transport was invoked by the assistant.
The current shared-grant identity and request signatures are tested with mocks,
which are explicitly software fixtures, never fresh Kimi evidence.

Prospective source quality does not establish forecast skill. Actual mature
forecasts, adequate calendar coverage, calibration and after-cost execution
comparisons remain necessary before a separately reviewed promotion. The caps
50/20/15/5/10 are experimental governance choices, not estimated optimal weights.
Native balances exclude tokens/staking/DeFi; public attribution does not prove
current control or personal-versus-fund capital. Declared research costs are
assumptions and do not come from an authenticated account fee query.

At the original evidence release, all implementation work was on
`codex/evidence-strategy-shadow`, based on
`59396f2` (`codex/independent-whale-shadow`). Git integration is the owner's
final choice under the finishing-a-development-branch skill. No merge into the
base or live settings change had occurred. The missing repository HANDOFF.md
was disclosed rather than reconstructed. The owner's subsequent main/bot/panel
deployment request and its rollout procedure are recorded in
`MAIN_PANEL_RELEASE_2026-10-12.md`; experimental live influence remains off.
