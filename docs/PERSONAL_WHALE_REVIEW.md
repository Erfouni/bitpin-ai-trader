# Personal-wallet pilot verification

Verified 11 October 2026, on `codex/independent-whale-shadow`, based on `4f31a03`.

## Review and regression checks

A fresh reviewer inspected the collector, report, CLI, units, tests, research notes and reused observation/lock helpers. Three material findings were reproduced with failing tests, fixed, and reviewed again:

1. A finalized-height rollback with an unavailable old height now starts a new comparison epoch only after both providers agree on a stable current anchor and balance. It does not query an unavailable higher old block. Disagreement and an unstable anchor do not advance the epoch.
2. Each change card displays both actual receipt and actual block elapsed intervals, which may differ because of finalization lag.
3. State setup rejects Windows reparse ancestry and checks resolved ancestor markers, preventing a junction into another collector's directory from creating personal state there. The actual Windows junction test passes on Python 3.7 and 3.14.

The reviewer confirmed the fixes and reported no unresolved findings. Public labels, current ownership proof and predictive profitability are distinct questions; this review establishes neither ownership nor a trading edge.

## Executed validation

| Runtime | Full suite | Result |
|---|---:|---|
| Python 3.7, Windows | 1,945 tests | Pass, 24 platform skips |
| Python 3.14, Windows | 1,945 tests | Pass, 24 platform skips |
| Python 3.12, hk staging | 1,945 tests | Pass, 2 platform skips |

The 29 focused personal-wallet tests pass on both Windows runtimes, with one Windows symlink privilege skip. Linux runs that symlink test and skips the Windows junction regression. Report-only tests make no network calls. All full-suite trading/account/network scenarios use the repository's test fixtures; no authenticated exchange call or live order was used for verification.

Fifteen runtime/unit/test file hashes were checked against the local source before the final server suite. `git diff --check HEAD` and systemd unit verification passed. Systemd emitted an unrelated existing xray user warning; no xray configuration was changed.

The final local collector also returned `ok`: both providers agreed at finalized block 26,170,123 on exactly 224,209.106255644280552032 ETH, making 12 requests including previous-anchor checks. Daily and weekly changes were unknown, with zero orders, zero model calls and `live_enabled=false`. Desktop and 390-pixel mobile views of the actual report were visually inspected; its content fits the mobile viewport.

## Server verification

Installed the isolated runtime under `/opt/bitpin-personal-whale-shadow`, with its own state under `/var/lib/bitpin-personal-whale-shadow`. All 13 installed code/font/unit hashes match the verified local source. State belongs to the existing `bitpin-flow` UID/GID 987/981. The service's effective inaccessible paths exclude the live bot, other collectors and xray; its only explicit writable state path is its own directory.

The first manual service invocation succeeded. Starting the timer also triggered its elapsed boot schedule once; that follow-up completed successfully at 15:50:36 UTC. Both providers agreed on finalized block 26,170,123 and the same exact native ETH balance shown above. The follow-up made 12 requests, including checks of the previous anchor. Daily/weekly reference values remained null. The service is a completed oneshot (`inactive/dead`, `Result=success`); the timer is `enabled`, `active/waiting`, with the next collection an hour after completion.

The main bot retained PID 27194 and its start time of 17:33:28 Tehran time. The flow service retained PID 8845 and its prior start time; DefiLlama and BTC whale timers retained their prior active state. None of those services was restarted. No account configuration, credentials, live signal or execution code was changed.

This is a prospective, single-wallet sample. Eight-day rolling observation storage is not a long-term predictive study, and zero live decision effect is intentional. Current beneficial ownership, complete wealth and transfer motives remain outside its verified scope.
