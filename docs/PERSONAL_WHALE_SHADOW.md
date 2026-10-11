# Publicly attributed personal-wallet pilot

The separate Ethereum experiment monitors the `Vb 3` Safe (`0x220866b1a2219f40e72f5c628b65d54268ca3a9d`), publicly attributed to Vitalik Buterin. Etherscan publishes its name tag and verified Safe-proxy contract; Arkham's August 2026 research describes Vitalik as an individual ETH holder and distinguishes individuals from exchanges/institutions. This is a labelled personal-wallet sample, not a verified complete inventory or representative independent-whale cohort. Current ownership has not been cryptographically reverified. The report exposes that limitation.

Both PublicNode and Flashbots returned 224,209.106255644280552032 native ETH in an initial read-only finalized-state research probe. Production collection pins both balance queries to the same agreed finalized mainnet block, verifies its hash/timestamp before and after, checks the previous successful anchor, and requires both balances to agree. A provider discrepancy, wrong chain, stale finalization, RPC error, cooldown, interrupted run or chain warning hides usable metrics. Finalization depends on the consensus reported by these providers; this is not independent light-client proof verification.

## Daily observations

The pilot records exact integer wei, receipt and block timestamps, and observed 24-hour/seven-day native-ETH stock changes. References must be successful observations in the same chain epoch within one hour of the receipt target; actual receipt and block elapsed times are included. New history is unknown until roughly a day/week has elapsed. Changes include transfers, fees and funding; they do not establish buying/selling, exchange deposits, donations, motives or gross inflow/outflow. ERC-20 assets, staking, DeFi positions and other wallets are excluded. No transaction history, trading trigger, calibrated whale threshold or predictive-edge claim is included.

The possible daily value is detecting material changes in a publicly attributed large holder's liquid native balance for later comparison with an unchanged baseline. A single ecosystem founder often funds projects or charities and is not representative of independent active traders. Observe prospectively, preserve receipt times and assess next-day information over multiple regimes before proposing any live use. SQLite retains eight days: export snapshots before expiry for a longer study. Automated predictive evaluation/archival is not part of this initial collector.

## Free-source investigation (11 October 2026)

| Source | Finding | Decision |
|---|---|---|
| PublicNode + Flashbots | Both tested key-free; Flashbots documents 200 balance calls/IP/5 minutes and 600 other calls/IP/5 minutes | Low-volume hourly native-ETH observation; no free uptime/SLA guarantee |
| Etherscan public name tags + Arkham public research | Attribution can distinguish a named individual profile from exchange custody; it is not current ownership proof | Pin the reviewed address and disclose source/date; do not scrape these sites hourly |
| Vitalik's 2018 self-published address | Official X oEmbed was readable; its link resolves to `0xab5801a7d398351b8be11c439e05c5b3259aec9b`; current finalized balance was about 16.68 ETH | Excluded from the current large-wallet pilot; do not select by old holdings |
| Nansen API | Current Free plan: 100 initial credits, then daily top-up to a 10-credit balance; all Pro endpoints available. Basic balance queries cost 1, smart-money queries 5, ordinary labels 100 and premium labels 500. A key/account is required | Potential later owner-configured source; no signup, key or paid call used |
| Nansen smart-money holdings | Aggregated token holdings of qualified traders, explicitly excluding whales/large holders/influencers; not a per-wallet breakdown | Does not itself fulfill direct independent-whale tracking |
| Anonymous large BTC addresses / rich lists | Large balances alone do not exclude exchange custody, pools or undisclosed common ownership | Not labelled as independent whales |
| Hyperliquid public user API/leaderboard | Positions and account roles can be public; user/vault classification does not prove independent beneficial ownership or skill | Distinct derivatives-account candidate for later research; not silently substituted for wallet tracking |
| Safe owner calls | Public `eth_call` probes returned 403; official docs define read-only owners/threshold access but do not guarantee access through these routes | Stop the failed approach; no bypass/retry, and no claim of current ownership verification |

No source proves a profitable daily rule for this sample.

## Isolation and operation

- Code: `/opt/bitpin-personal-whale-shadow`
- State: `/var/lib/bitpin-personal-whale-shadow`
- Outputs: `personal-whale-shadow.json`, `report.html`
- Service/timer: `bitpin-personal-whale-shadow.service`, `bitpin-personal-whale-shadow.timer`

The marked directory, SQLite history and process lock are separate from the BTC pilot. The hardened hourly oneshot uses the existing `bitpin-flow` public-data user, cannot access live bot config/state or other collectors' state, and does not restart the main bot. No live settings, account client, model context or order logic changes. Every snapshot states shadow mode, no live effect, zero orders and zero model calls.

Transport allows only `eth_chainId`, `eth_getBlockByNumber` and the fixed address's `eth_getBalance` requests. Although JSON-RPC uses HTTP POST, these are read-only methods. Submission, signing, owner calls and arbitrary addresses are rejected before networking. HTTPS uses stock certificate/hostname verification with the existing per-connection IPv4 adapter; no host networking changes. Responses are limited to 1 MiB and validated for RPC version/ID/errors, canonical hex, duplicate JSON keys, nonfinite values and excessive nesting. Each run makes at most 20 calls with two-second spacing; provider cooldowns persist for at least one hour and honor longer bounded Retry-After delays. There is no immediate network retry.

Before networking, the collector persists and publishes `collecting`/unknown so a timeout cannot revive an older successful report. Receipt age over 9,000 seconds or a future receipt suppresses metrics. Static HTML describes its displayed receipt; regenerate old copies before treating them as current.

```sh
python3 -B scripts/personal_whale_shadow.py --state-dir state/personal-whale-shadow
python3 -B scripts/personal_whale_shadow.py --state-dir state/personal-whale-shadow --report-only
systemctl status bitpin-personal-whale-shadow.service bitpin-personal-whale-shadow.timer --no-pager
journalctl -u bitpin-personal-whale-shadow.service -n 10 --no-pager
```

Exit 0: agreed usable observation. Exit 1: unknown/degraded. Exit 2: local directory/database/output failure. Stop independently with `sudo systemctl disable --now bitpin-personal-whale-shadow.timer`. Live integration requires a separate reviewed proposal and owner authorization.

## Primary sources

- [Etherscan Vb 3 address/tag](https://etherscan.io/address/0x220866b1a2219f40e72f5c628b65d54268ca3a9d) — public HTML checked directly; the web fetch tool could not render it.
- [Arkham's individual-holder research](https://info.arkm.com/research/vitalik-buterin-net-worth)
- [Vitalik's self-published 2018 address](https://x.com/VitalikButerin/status/1050126908589887488) — checked through the official public oEmbed response and short-link redirect.
- [Ethereum JSON-RPC contract](https://ethereum.org/developers/docs/apis/json-rpc/)
- [PublicNode public endpoints](https://publicnode.com/)
- [Flashbots default RPC](https://docs.flashbots.net/flashbots-protect/settings-guide) and [read-method rate limits](https://docs.flashbots.net/flashbots-protect/ratelimiting)
- [Safe read-only owners](https://docs.safe.global/reference-smart-account/owners/getOwners)
- [Current Nansen free credits and endpoint costs](https://docs.nansen.ai/getting-started/credits)
- [Nansen holdings scope](https://docs.nansen.ai/api/smart-money/holdings)
- [Hyperliquid public user API](https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/info-endpoint)
