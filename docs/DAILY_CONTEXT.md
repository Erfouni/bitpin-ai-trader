# Daily context and response robustness

The allocator can opt in to `context.defi_daily_snapshot`, an absolute path to the independent collector's
`defi-daily.json`. The default is `null`, so installing code alone preserves the existing analysis inputs.
The collector remains isolated: its `shadow` mode, zero model calls and zero orders describe the collector;
the main bot reads its observations only after the owner enables and confirms this setting.

The new context contains six global indicators and TVL, DEX volume and USD-pegged stablecoin supply for
Ethereum, Solana, BSC, Arbitrum, Sui and Tron. It uses closed UTC days, exact-date 1/7/30-day changes and,
for daily volume/fees/revenue, complete 7/30-day means and week-over-week comparisons. Rates are
recomputed from bounded history. Missing days are unknown. Receipt age must not exceed 2.5 hours;
the latest daily bucket must not exceed 72 hours. Freshness is checked again after context processing.
Invalid sources are isolated; malformed JSON, wrong state markers, oversized files and symlinks are rejected.
Only fixed source names and validated numbers enter the context. Prompt size controls remove chain detail
first, then optional context if necessary, retaining existing portfolio and strategy information.

These are supporting observations, with no calibrated probability bonus or additional decision trigger.
TVL changes include price effects; DEX volume is turnover; stablecoin supply is a liquidity proxy; fees and
revenue describe covered protocol activity; open interest is an exposure stock with unknown direction.
They do not identify smart-money wallets or exchange deposits/withdrawals, and correlated observations
must not count as independent confirmation. Existing validation, P RULE, fees, stops and two-vote buy
confirmation remain in force. Definitions follow [DefiLlama's documentation](https://docs.llama.fi/analysts/data-definitions).

A malformed second confirmation can use one unused validation retry, within the existing decision deadline
and daily call/token reserves. Its fresh input contains the original context and fixed schema instructions;
neither allocation proposal nor rejected values are echoed. Truncated, empty, failed, semantically invalid
or still-malformed confirmation cannot approve a buy. The final report reflects the allocation after cuts.

News content-safety refusals stop immediately; they never change JSON/streaming settings or trigger a
provider/content workaround. When trusted feeds were already fetched, the bot can provide sanitized
dated/linked headlines, explicitly labeled degraded, without a model summary or invented implications.
Reuse is capped at 60 minutes; failure cooldown, original outage start and last model success are retained.
The panel and notification health continue to show the news model outage. See
[Kimi's error documentation](https://platform.kimi.ai/docs/api/errors).

## Owner activation on the server

After installing this version, run:

```bash
sudo bash /opt/bitpin-bot/deploy/enable_defi_daily.sh
```

This interactive helper checks fresh global data and the existing confirmation, backs up only non-secret
settings and the confirmation record, stops the bot, adds the one context key, runs `bitpin-bot check`,
asks the owner through `bitpin-bot confirm-live`, and starts the bot. The owner types the phrase; the
helper does not supply it. Canceling or failing restores the old files atomically, checks their matching
confirmation, and restores the previous running state. A repeat with the same confirmed setting changes
nothing. No credentials, authenticated exchange calls or extra model test calls are made by the data preview.
