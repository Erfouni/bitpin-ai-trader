# Publicly associated personal-wallet pilot

The separate Ethereum experiment monitors three addresses from an immutable reviewed catalog: the existing `Vb 3` Safe associated with Vitalik Buterin, Patricio Worthalter's historical Arbitrum Security Council address, and Etherscan's `Arthur Hayes 4` address. Etherscan publishes the Vitalik name tag and verified Safe-proxy contract; Arkham's August 2026 research describes Vitalik as an individual ETH holder. Patricio's official historical association is strong, while current control and personal funds remain unverified. Arthur's public attribution is medium, with no address self-disclosure found and ambiguity between personal and fund capital. This is a selective public-association sample, not a verified complete inventory or representative independent-whale cohort. Current ownership has not been cryptographically reverified. Every card exposes its own attribution source and limitations. See [additional-wallet research and fixed-block probe](ADDITIONAL_PERSONAL_WALLETS.md).

Both PublicNode and Flashbots returned 224,209.106255644280552032 native ETH for Vb 3 in the initial research probe. The additional-wallet probe is separately timestamped in the research document. Production collection pins every wallet's two balance queries to one agreed finalized mainnet block, verifies its hash/timestamp before and after, checks the previous successful anchor, and requires every exact wei balance to agree. A discrepancy for any wallet, wrong chain, stale finalization, RPC error, cooldown, interrupted run or chain warning hides all usable metrics. Finalization depends on the consensus reported by these providers; this is not independent light-client proof verification.

## Daily observations

As of 3.14.1, production uses PublicNode and the keyless public dRPC gateway after repeated Flashbots balance-read
timeouts. The historical probe above retains its original source attribution. Current and legacy receipt pairs are
recognized explicitly; gateway agreement does not prove independent underlying operators. See
[diagnosis, constraints and recovery](ETH_RPC_RECOVERY.md).

The pilot records exact integer wei, receipt and block timestamps, and each wallet's observed 24-hour/seven-day native-ETH stock changes. A reference must contain that wallet's measured balance in a successful observation in the same chain epoch within one hour of the receipt target. Each row includes the prior balance, receipt, anchor and actual receipt/block elapsed times. New history is unknown until roughly a day/week has elapsed. A measured zero is a real reference; a missing balance is unknown. Changes include transfers, fees and funding; they do not establish buying/selling, exchange deposits, donations, motives or gross inflow/outflow. ERC-20 assets, staking, DeFi positions and other wallets are excluded. No transaction history, trading trigger, calibrated whale threshold or predictive-edge claim is included.

The original `address`, `attribution`, `balance_wei`, `change_24h_wei`, `change_7d_wei` and interval scalar fields retain their Vitalik meanings. The additive `wallets` array contains all three reviewed rows. Existing SQLite records remain intact: only Vitalik can inherit the old scalar balance. Missing Patricio/Arthur balances and references are not populated from the research probe, guessed, zero-filled or backdated. The old CLI options and exit codes remain supported.

The possible daily value is detecting changes in publicly associated large addresses' liquid native balances for later comparison with an unchanged baseline. These three profiles may fund projects, charities or managed investments and are not representative of independent active traders. Observe prospectively, preserve receipt times and assess next-day information over multiple regimes before proposing any live use. SQLite retains eight days: export snapshots before expiry for a longer study. The collector itself does not evaluate a predictive strategy.

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

Transport allows only `eth_chainId`, `eth_getBlockByNumber` and `eth_getBalance` for the three immutable catalog addresses at an explicit height. Although JSON-RPC uses HTTP POST, these are read-only methods. Submission, signing, `eth_call`, owner calls and arbitrary addresses are rejected before networking. HTTPS uses stock certificate/hostname verification with the existing per-connection IPv4 adapter; no host networking changes. Responses are limited to 1 MiB and validated for RPC version/ID/errors, canonical hex, duplicate JSON keys, nonfinite values and excessive nesting. A normal first run uses 14 calls; a run checking a previous anchor uses 16. The ceiling remains 20 with two-second spacing; provider cooldowns persist for at least one hour and honor longer bounded Retry-After delays. There is no immediate network retry.

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
