# Bitpin and DefiLlama shadow flow collector

The owner approved implementing the researched Bitpin + DefiLlama proposal in experimental mode first. Real allocation decisions and orders require a later approval after results.

## Scope and contract
- Standalone public-data collector; no imports of the authenticated client, broker, brain or runner.
- No live config/state access, no credentials, no model calls, orders or order cancellation.
- Python 3.7 through 3.14, standard library only.
- Start with REST polling every 30 seconds, five markets (BTC_USDT, ETH_USDT, SOL_USDT, XRP_USDT, USDT_IRT). This is an observable-data MVP, not event-complete order flow. Detect absent overlap, malformed rows and timing gaps; never claim coverage across them.
- Persist trade IDs, events, receipt times, book snapshots, poll errors, continuity and liquidity snapshots in a separate SQLite database; bounded retention.
- Per-market 5m, 15m, 1h, 4h, 24h taker buy/sell quote notional, delta and buy ratio. Only continuous fully warmed windows expose usable signals. No common-currency sum across markets.
- Show observed order-book depth and imbalance at 0.1%, 0.5%, 1%; flag truncation rather than assuming full depth.
- DefiLlama USDT and USDC circulation changes versus provider previous-day/week/month fields; fetch hourly, cache with explicit receipt age, never invent a provider timestamp. Supply is a liquidity proxy, not exchange inflow or proof of buying.
- Persist latest machine-readable snapshot and a polished self-contained Persian RTL HTML report. Reports explicitly say shadow-only and show missing/stale/warming data.
- Public transport permits only the selected HTTPS hosts and GET paths, blocks redirects, ignores proxy/credential environment, bounds response size/time and observes Retry-After on 429.
- CLI defaults to one collection; bounded runs and explicit continuous operation supported. Reject live flags and shared bot state directories.
- Offline replay and live public-data smoke test on hk. A short smoke test proves plumbing/quality only, not profitability.
- No decision-engine or live-service activation in this change.

## Source contracts
Bitpin matches: https://docs.bitpin.ir/v1/docs/market-data/matches
Taker meaning: https://docs.bitpin.ir/v1/docs/websocket/matches
20-row book: https://docs.bitpin.ir/v1/docs/market-data/orderbook
DefiLlama: https://api-docs.defillama.com/

## Acceptance
Tests cover duplicate and conflicting IDs, out-of-order events, missing overlap, outages/restarts, malformed/future/nonfinite data, depth truncation, independent source failure, schema drift, HTTP limits and no-trading isolation. Complete suite on Python 3.7 and 3.14; target Python 3.12 smoke test; inspect rendered report.
