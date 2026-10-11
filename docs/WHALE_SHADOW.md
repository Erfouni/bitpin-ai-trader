# Public Bitcoin wallet observation pilot

This experiment observes three historically published Bitfinex BTC custody addresses. It is **shadow only**: it is not connected to Kimi's context or trade decisions and cannot submit orders. It requires no API key or paid subscription.

## Research verdict

Free public explorers provide useful raw blockchain observations, but dependable ongoing free identification of independent investor whales across many chains was not established. Accurate transfers and accurate ownership/motive attribution are different requirements.

| Source | Free access assessed | Suitability |
|---|---|---|
| Blockstream Esplora | Public read-only address and transaction endpoints, no key required in the tested route | BTC pilot primary source; anonymous limits/SLA are not guaranteed |
| mempool.space | Public REST explorer endpoints, no key required in the tested route | Independent cross-check; obey throttling, no unlimited-access claim |
| Bitfinex official published addresses | Public wallet list pinned to its 2022-12-12 revision | Historical self-attribution only; current control not cryptographically reverified |
| Whale Alert | Free website shows a selection of alerts; continuous API access is paid, with a trial | More attribution, but does not meet the ongoing free-first requirement |
| Arkham | API requires onboarding/key/credits; guaranteed ongoing free attributed access not established | Possible later upgrade, not a dependency of this experiment |
| Blockchair | Free testing/noncommercial policies and documentation on keys differ | Avoid relying on anonymous access for a permanent application |
| Etherscan / Alchemy | Free tiers require keys; raw logs still need ownership attribution | Potential later chain extension with owner-approved access; not implemented |

Research was checked on 2026-10-11. No subscription, account signup, API credential, or paid endpoint was used.

Studies of large BTC transfers find different reactions for different transfer types and horizons. Transfers can reflect internal wallet management, collateral, settlement, purchases, or sales. Results from a broader attributed feed cannot be assumed to apply to this three-address sample. Daily exchange-reserve research provides a reason to test daily observations, not proof of a profitable trading rule.

## What is collected

- Confirmed chain balance per address, in exact integer satoshis. This includes transactions with at least one confirmation; pending balances are excluded. Pending transaction counts are shown separately in JSON per explorer.
- Observed balance changes near 24 hours and seven days. The reference must be a successful observation in the same chain epoch within one hour of the desired reference time. Actual elapsed seconds are recorded. No interpolation or fabricated zeros.
- Up to two pages of confirmed transactions per address per explorer (50 per address); IDs are deduplicated across the published set. This is a recent bounded sample, not complete daily transaction-flow coverage.
- Transactions with fewer than six confirmations are excluded from the eligible sample. Six confirmations are a research guard, not absolute finality.
- Movements within the published address set, from/to unlisted addresses, and ambiguous input/script patterns. Mixed inputs and coinbase transactions remain ambiguous. Unlisted addresses may belong to the same owner, including undisclosed change addresses.
- A 500 BTC exploratory large-movement flag on the classified address-set amount. It is not a calibrated threshold, a buy/sell trigger, or an assertion that the address is an independent investor whale. Internal transfers can be large too.

Balances, daily references and economic transaction fields must agree between both providers. The collector checks changing address counters, pagination duplicates/order, transaction relevance, exact fee conservation, shared canonical chain anchors, source tip age and persisted provider cooldowns. A detected reorganization starts a new comparison epoch. No usable metrics are published after an error, disagreement, stale observation or reorganization warning.

Requests are HTTPS GET only, with a fixed endpoint/address allowlist, no redirects or environmental proxies, a two-second pause, at most 64 requests per run and no immediate network retry. The collector resolves IPv4 addresses per HTTPS connection: unavailable IPv6 routes on the target host otherwise caused long fallback delays. TLS still verifies the original hostname and certificate; no global resolver, networking setting or other service is changed. Provider 429/503 cooldowns are persisted. The hourly experiment should remain small; repeated throttling requires reducing its frequency or self-hosting an explorer rather than bypassing quotas.

## Isolation and operation

Runtime paths:

- Code: `/opt/bitpin-whale-shadow`
- State: `/var/lib/bitpin-whale-shadow`
- JSON: `/var/lib/bitpin-whale-shadow/whale-shadow.json`
- Offline report: `/var/lib/bitpin-whale-shadow/report.html`
- Service/timer: `bitpin-whale-shadow.service`, `bitpin-whale-shadow.timer`

The hardened oneshot runs hourly as the existing `bitpin-flow` user. Its sandbox denies access to bot configuration/account state, the live application, xray configuration and the other collectors' state. The collector uses its own directory marker, lock and SQLite history, retained for eight days. It does not read any trading settings or restart the main bot.

Local experiment:

```sh
python3 -B scripts/whale_shadow.py --state-dir state/whale-shadow
python3 -B scripts/whale_shadow.py --state-dir state/whale-shadow --report-only
```

Exit 0 means an agreed, usable observation; exit 1 means unknown/degraded data; exit 2 means a local directory/database/output error. `--report-only` makes no network requests and recalculates freshness. A JSON receipt older than 9,000 seconds or in the future is stale. The static HTML describes the displayed receipt time; regenerate it before treating an older saved report as current.

Server checks:

```sh
systemctl status bitpin-whale-shadow.service bitpin-whale-shadow.timer --no-pager
journalctl -u bitpin-whale-shadow.service -n 15 --no-pager
python3 -c 'import json; p=json.load(open("/var/lib/bitpin-whale-shadow/whale-shadow.json")); print(p["status"],p["sources"],p["live_enabled"])'
```

Stop this experiment independently with `sudo systemctl disable --now bitpin-whale-shadow.timer`. No trade configuration changes or live-confirmation phrase are needed for this isolated pilot.

## Evaluation before any live proposal

The first 24-hour comparison needs approximately a day of collection, and a seven-day reference needs a week. SQLite keeps a rolling eight-day history: export public snapshots before they expire if conducting a longer study. Automated archival or predictive evaluation is not part of this initial pilot. Preserve receipt times and source revisions; historical transactions in the first sample are backfill and must not be treated as information first observed in the past. For predictive evaluation, use the observation's receipt time, not its block time.

Test whether these observations improve next-day BTC return/volatility information beyond the existing price/volume context. Use future observations, walk-forward evaluation, an unchanged baseline and several market regimes; account for fees and avoid selecting thresholds using the final evaluation period. Missing or disputed observations must remain missing. This pilot has no established predictive effect yet. Any eventual live integration needs a separate reviewed proposal and owner authorization.

## Primary references

- [Esplora API contract](https://github.com/Blockstream/esplora/blob/master/API.md)
- [mempool.space REST documentation](https://mempool.space/docs/api/rest)
- [Pinned official Bitfinex publication](https://github.com/bitfinexcom/pub/blob/83d06bdb5f5a2ce05d5ba2db5a426dd44c9f5128/wallets.txt)
- [Bitfinex transparency announcement](https://blog.bitfinex.com/announcements/bitfinex-resilient-in-face-of-market-events-committed-to-greater-transparency-and-demonstrating-proof-of-reserves/)
- [Blockstream's 2026 API/free-tier guidance](https://blog.blockstream.com/enhanced-analytics-and-infrastructure-improvements-for-the-blockstream-explorer-api/)
- [Whale Alert API documentation and access plans](https://developer.whale-alert.io/api-account/documentation)
- [Whale Alert free-site limitations](https://whale-alert.io/faq.html)
- [Arkham API documentation](https://arkm.com/docs)
- [Blockchair API policy](https://github.com/Blockchair/Blockchair.Support/blob/master/API_DOCUMENTATION_EN.md)
- [Python HTTPS handler extension API](https://docs.python.org/3/library/urllib.request.html#urllib.request.HTTPSHandler) and [CPython connection implementation](https://github.com/python/cpython/blob/3.14/Lib/http/client.py)
- [Ante & Fiedler: Market Reaction to Large Transfers on the Bitcoin Blockchain](https://www.blockchainresearchlab.org/wp-content/uploads/2020/03/Market-Reaction-to-Large-Transfers-on-the-Bitcoin-Blockchain-Do-Size-and-Motive-Matter-BRL-Working-Paper-9.pdf)
- [Loaded for bear: Bitcoin private wallets, exchange reserves and prices](https://www.sciencedirect.com/science/article/pii/S0378426622002023) — published abstract assessed; publisher blocked automated full-text access, so no full-text claims are made.

No source above establishes a profitable daily signal for this exact sample.
