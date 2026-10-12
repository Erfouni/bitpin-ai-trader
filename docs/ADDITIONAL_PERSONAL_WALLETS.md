# Additional Ethereum wallet associations

Research checked on 11 October 2026. These three catalog entries are publicly associated addresses. None establishes current control, independent beneficial ownership, a complete portfolio, trading skill or a profitable signal. The catalog is immutable; collection does not scrape labels or accept arbitrary addresses. This document records research provenance, not historical collector receipts.

| Profile | Address | Evidence and limits |
|---|---|---|
| Vitalik Buterin · Vb 3 | `0x220866b1a2219f40e72f5c628b65d54268ca3a9d` | Existing [Etherscan public label](https://etherscan.io/address/0x220866b1a2219f40e72f5c628b65d54268ca3a9d) and [Arkham profile research](https://info.arkm.com/research/vitalik-buterin-net-worth). Current control unverified; holdings outside this Safe excluded. |
| Patricio Worthalter | `0xf6b6f07862a02c85628b3a9688beae07fea9c863` | Strong historical association: the [Arbitrum Foundation initial-setup transparency report](https://docs.arbitrum.foundation/foundational-documents/transparency-report-initial-foundation-setup) lists Patricio and this address in its initial Security Council March cohort. The document expressly distinguishes the initial membership from current membership. This does not establish current control or prove funds are personal. |
| Arthur Hayes · Arthur Hayes 4 | `0x6cd66dbdfe289ab83d7311b668ada83a12447e21` | [Etherscan's address label](https://etherscan.io/address/0x6cd66dbdfe289ab83d7311b668ada83a12447e21), complemented by [Arkham research published 19 June 2026](https://info.arkm.com/research/arthur-hayes-net-worth-crypto-holdings-2026). Medium public attribution; no self-disclosure found for this address. Personal versus fund capital is ambiguous, and current control is unverified. Arkham discusses both identified holdings and Hayes' family-office fund; it does not resolve this wallet's beneficial capital ownership. |

The foundation page was independently read during implementation. The Arthur research page's visible publication date is 19 June 2026; its search/page metadata uses a different title date. The Etherscan web reader could not render the Arthur address; the coordinating researcher's direct public-HTML check supplied the exact label. No authenticated analytics account, API key, signing request or owner call was used.

## Recorded finalized-state research probe

The coordinating research probe completed at `2026-10-11T16:11:35Z` using PublicNode and Flashbots. It used 14 public read-only calls and checked a stable common finalized block for the candidate checks. Both providers agreed on the additional exact native balances below.

- Mainnet block: `26170251`
- Hash: `0x412b762ede6be6e81028b42d4443dda9d58efb9c4bf8ea52c81e7865ffd1a085`
- Block timestamp: `2026-10-11T15:57:11Z`

| Candidate | Exact wei | Exact native ETH | Decision |
|---|---:|---:|---|
| Patricio Worthalter | 1034946612288207873404 | 1,034.946612288207873404 | Add with historical/control/personal-funds limits |
| Arthur Hayes 4 | 8013538388781412542190 | 8,013.538388781412542190 | Add with attribution/self-disclosure/fund-capital limits |
| Stani candidate | 0 | 0.000000000000000000 | Exclude from native-ETH expansion; AAVE holdings were not measured |

Vb 3's earlier initial probe recorded `224,209.106255644280552032 ETH`, as documented in [the existing pilot](PERSONAL_WHALE_SHADOW.md). That earlier value is not asserted to be a measurement at the candidate-probe block. The test fixtures use invented controlled balances strictly as offline software tests; they are never published as research receipts.

## Collection and migration

Each run validates mainnet and both finalized heads, chooses one common height, reads all three exact balances from each provider, checks the previous successful anchor when available, and rechecks the common anchor after the reads. All balances must agree. Any disagreement, error, cooldown, unstable anchor or reorganization suppresses the entire snapshot's usable metrics. First runs use 14 requests; normal subsequent runs use 16; the hard ceiling is 20. Calls are spaced two seconds apart, responses are at most 1 MiB, and provider-error cooldowns last at least one hour.

The `wallets` rows expose exact current/native changes and each wallet's own reference balance, receipt, block and actual elapsed intervals. Historical rows without a new member are unknown for that member; a real measured zero remains valid. The original Vitalik scalar fields and CLI options remain compatible. No old records are rewritten and no probe values are inserted into missing collector history. New members begin with unknown daily/weekly deltas until valid observations exist near each target interval in the same chain epoch.

ERC-20 tokens, staked positions, DeFi claims and other addresses are outside coverage. Balances measure stocks; changes can reflect transfers, fees, donations or funding and do not identify buys, sells, motives or gross flows. Two RPC providers' agreement is not a separately verified consensus light-client proof. Address association is not evidence of independent ownership, and adding profiles does not validate their predictive value.
