# شواهد عمومی در تحلیل واقعی کیمی / Public evidence in live Kimi context

نسخهٔ 3.14.0 داده‌های عمومی را در همان مسیر تحلیل روزانهٔ کیمی مصرف می‌کند. بیت‌پین و DefiLlama از کلیدهای اختیاری
قبلی استفاده می‌کنند؛ برای مشاهدات کیف‌پول، دو کلید تازه و پیش‌فرض `null` اضافه شده است. نصب کد به‌تنهایی این دو کلید
را فعال نمی‌کند. یادگیری وزن‌های پیش‌بینی همچنان در حالت پژوهشی است؛ این نسخه برتری سودآوری را اثبات نمی‌کند.

English: this release adds opt-in native wallet observations to the existing allocator. Data can corroborate
or contradict its daily analysis. The research forecast mixture is not promoted, and no calibrated wallet
probability bonus or profitable edge is claimed. Kimi remains the decision maker under existing execution rules.

## برنامهٔ اجرا / Implementation sequence

1. Validate the isolated public receipts against their catalog, source identities, timestamps and units.
2. Add explicit nullable context paths; retain all existing probability, fee, confirmation and position rules.
3. Recheck facts at every model call, including retries and the second buy vote; preserve the context size bound.
4. Record only sanitized delivery states and usable counts; display them separately from research weights.
5. Test offline on Python 3.7, 3.14 and the deployment server; review code and inspect desktop/mobile rendering.
6. Synchronize the maintained private source and public release; deploy code with rollback available.
7. The owner enables the new settings interactively, then verifies delivery after the next scheduled decision.

## فعال‌سازی / Owner activation

After the new code is installed, the owner runs:

```bash
sudo bash /opt/bitpin-bot/deploy/enable_wallet_context.sh
```

The script previews only public observations, checks the existing confirmation, saves a private settings and
confirmation backup, stops the bot, writes the two explicit paths, runs `bitpin-bot check`, asks the owner to
complete `bitpin-bot confirm-live`, and starts the bot. Cancellation/failure restores the previous settings and
confirmation and the prior running state. The assistant must never enter the owner's confirmation phrase.
The script requires an interactive terminal and the installed deployment path. No credential is supplied to it.
The full activation and rollback hold the same mutation lock as the panel, so concurrent settings changes
cannot be overwritten. Direct Python apply/restore entries acquire the same lock.

برای اجرای مستقیم از کامپیوتر مالک، در ترمینال تعاملی:

```bash
ssh -t YOUR_SERVER_ALIAS 'sudo bash /opt/bitpin-bot/deploy/enable_wallet_context.sh'
```

The two settings are:

```json
{
  "context": {
    "personal_whale_snapshot": "/var/lib/bitpin-personal-whale-shadow/personal-whale-shadow.json",
    "custody_whale_snapshot": "/var/lib/bitpin-whale-shadow/whale-shadow.json"
  }
}
```

This is a fragment, not a replacement for existing settings. The helper preserves other settings and comments.
A BTC disagreement does not block activation when fresh personal ETH observations exist; it remains excluded
until both BTC providers agree. Setting both paths to `null` disables wallet consumption and requires the same
owner check/confirmation/restart workflow. Collector services retain their isolated research mode.

## معنی داده و اثرگذاری / Meanings and influence

| Source | What may enter daily analysis | What it cannot establish |
|---|---|---|
| Bitpin | Fully covered observed 24h taker buys/sells, quote-specific pressure | Exchange deposits/withdrawals, trader identity, guaranteed complete REST event coverage |
| DefiLlama | Closed UTC daily TVL, gross DEX volume, stablecoin units, fees, revenue, open interest; available 7/30d trends | Net inflows, buy direction, identical economic exposure across correlated series |
| Publicly associated ETH addresses | Native balances at an agreed finalized block; available receipt-based 24h/7d changes | Current control, verified personal capital, all tokens/staking/DeFi, a purchase or sale |
| Historical BTC custody sample | Confirmed native balances and available receipt-based changes with two-provider agreement | Independent investors, complete exchange reserves, complete daily flows or absolute finality |

TVL in USD also changes with prices; DEX volume is gross turnover. See the primary
[DefiLlama metric definitions](https://docs.llama.fi/analysts/data-definitions).
Ethereum `eth_getBalance` reports an address balance at a block; it does not identify a trade. See the primary
[Ethereum JSON-RPC documentation](https://ethereum.org/developers/docs/apis/json-rpc/).
Address association sources and their uncertainty are in the installed public wallet catalog.

The new data supplies supporting/conflicting evidence only. It cannot create a P RULE setup or base-rate row,
raise probability beyond the existing permitted basis, trigger extra decisions, bypass confirmation, or add
model calls. Exact native integers and quote units stay intact. Custody and personal balances are not summed,
and related macro measurements or individual wallets are not counted as independent confirmations.

Research mixture ceilings are hypotheses, not calibrated live weights or enforceable percentages of Kimi
attention. Untrained weights remain zero. Existing model token meters and daily budgets still apply; longer
inputs can increase the cost of the already scheduled requests. No fresh paid evaluation is run by this release.

## تازگی، ثبت و پنل / Freshness and panel evidence

Wallet receipt age is at most 9,000 seconds; the ETH finalized anchor is at most 7,200 seconds old. The source
must match its isolated marker, public catalog and collector contract. Future, duplicate, malformed, stale or
disputed evidence is excluded independently. Receipt time is not a provider publication timestamp.
Daily changes use recorded references within one hour of the requested 24h/7d interval and remain unknown
when sufficient history is unavailable. Small windows do not become entry signals.

Before each existing model call the bot rechecks optional facts. If prompt limits require reduction, optional
wallet facts are dropped before core prices/portfolio fields. The delivery panel reports included, omitted and
absent source groups and their usable observation counts. `attempted` means a call was attempted; transport
can still fail or decline it. `response_received` means a model response returned, not that it was valid,
used a particular fact, caused an order, or earned a profit.

Only this fixed sanitized file is exposed to the research helper:

```text
/var/lib/bitpin-bot/live-evidence-status.json
```

It contains no account balances, positions, prices, keys, model response text or per-wallet amounts. Missing or
stale records show unknown delivery rather than implying settings are off. Check the next normal scheduled
decision after activation; do not spend an extra model call merely to update this panel.
