# Owner-run Kimi architecture experiment

The fresh experiment is **not_run** until the owner obtains valid real forecasts. Failed/unknown attempts still appear in attempted-call counts and retained reservations. Preparation and scoring are free, offline operations. Synthetic fixtures test the software and always remain `fixture_only`; they cannot become measured Kimi results. The live allocator, settings, account state, order rules, and existing Kimi budgets/logs are outside this experiment.

## Frozen comparison

Each predeclared episode supplies the same five historical technical features to both arms. `raw_history` additionally supplies 60 normalized, closed daily bars; `structured_packet` supplies the compact features and an optional validated probability/influence packet. The comparison measures whether retaining the extra history changes useful forecasts. It does not demonstrate that an omitted historical news, wallet, flow, or macro source was available. Optional packets must come from the point-in-time evidence pipeline; the runner cannot reconstruct their training history from probability vectors alone.

The event is `next_24h_down_flat_up` with the episode's declared cost band, currently 0.008 for the historical sample. Down means return below -0.8%, flat includes both boundaries, and up means return above +0.8%. This is separate from the live candidate probability of target-before-invalidation. A reply may contain a hypothetical spot target weight between zero and one; nothing places an order.

The allowlist contains finite numeric features, normalized historical values, probabilities, governance weights, group quality statuses, and relative bar ages. Prompts contain `asset_A`, generated evidence IDs, and the horizon/band. Symbols, episode IDs, calendar dates, addresses, original context, strategy knowledge, labels, and realized returns are never copied into requests. Blinding reduces recognition channels but cannot guarantee that a later-trained model lacks retrospective market knowledge.

Cases, arms, prompt version, model, reasoning effort, request signatures, data-input hashes, code hash, prices, and output bounds are registered before execution. One experiment requires a single explicit return currency and cost band; overlapping outcome intervals for the same asset are refused. There are at most 16 distinct paired cases, with zero, one, or two additional stability pairs on the first case: at most 36 calls. Duplicate inputs are one case. Successful cached replies are reused for free. Stability repeats measure disagreement and add no independent market observations. The runner never selects another variant after seeing an attractive percentage.

## First run the free pipeline

The measured pilot is already registered. Its frozen input is
`scratch/kimi-development-pilot.json`; do not overwrite it with a different
selection or regenerate its state. Preparation selected 16 development days
without using outcomes for selection, spaced by 30 days and rotating available
BTC/ETH/SOL episodes. It excluded the final 180-day quantitative holdout. Each
packet uses the model and calibration fit from that development fold's past
data, with zero influence if fewer than 30 calibration days were available.
The private reproduction script is `scratch/prepare_measured_pilot.py`; it uses
the frozen exports `scratch/strategy-episodes-USDT.json` and
`scratch/strategy-evaluation.json`. Keep the input receipts and code versions.

PowerShell:

```powershell
py -3.14 scripts/kimi_strategy_experiment.py prepare --episodes scratch/kimi-development-pilot.json --state-dir scratch/kimi-experiment-owner-v1 --max-cases 16 --stability-repeats 1 --reasoning-effort low
py -3.14 scripts/kimi_strategy_experiment.py score --episodes scratch/kimi-development-pilot.json --state-dir scratch/kimi-experiment-owner-v1
```

Identical preparation is idempotent. On Linux use `python3` in place of
`py -3.14`. The implementation supports Python 3.7–3.14 and uses only the
standard library. The prepared registration hash is
`b4ad67b08047d9c5a082dec99509ab16013ffa46f1cf31eec7dba80b00b455e1`.

Review `manifest.json` in this separate research directory and the reported `worst_case_usd` before spending. Preparation creates no model client and does not read environment variables or credential files. An existing nonempty unmarked directory, live/collector state directory, symbolic link, Windows junction/reparse path, or linked artifact is refused. Re-running identical preparation is idempotent; replacing the registered cases is refused. Do not reuse a fixture directory for owner execution.

## Owner-only fresh execution

The same pilot is prepared on `hk` in the separate, owner-private directory
`/root/bitpin-evidence-research-20261012`. Its ledger has zero calls and zero
reservations. Use this one machine/account for paid execution. These are the
exact owner commands, with an already available `KIMI_API_KEY` in the owner
shell environment:

```sh
ssh hk
cd /root/bitpin-evidence-research-20261012
python3 -B scripts/kimi_strategy_experiment.py execute --state-dir experiment-v1 --owner-execute --max-calls 2
python3 -B scripts/kimi_strategy_experiment.py score --episodes kimi-development-pilot.json --state-dir experiment-v1
```

This first pair reserves at most $0.384426. Inspect both validated responses
before continuing. A provider answer, useful forecast or improved strategy is
not guaranteed. Stop here if spending is conditional on certainty of success.
The assistant has never run `execute --owner-execute` with real transport.
Repository `CLAUDE.md` explicitly prohibits the assistant from reading or using
the Kimi key, so actual paid execution remains the owner's step.

After a useful valid pair, the owner can finish only the same registered pilot:

```sh
python3 -B scripts/kimi_strategy_experiment.py execute --state-dir experiment-v1 --owner-execute
python3 -B scripts/kimi_strategy_experiment.py score --episodes kimi-development-pilot.json --state-dir experiment-v1
```

Keep the server research artifacts private. The static public shadow report
does not automatically import owner responses; its `not_run` notice describes
the delivered free evaluation until a separate verified result is imported.

Only the owner runs this command. It uses an **already existing** `KIMI_API_KEY` in the owner's process environment. Never paste the key into this chat, a command argument, the episode JSON, or the experiment directory.

```powershell
py -3.14 scripts/kimi_strategy_experiment.py execute --state-dir scratch/kimi-experiment-owner-v1 --owner-execute --max-calls 2
```

The first two calls form the first registered pair. Continue the same predeclared experiment only if the pair is valid and useful for the intended feasibility comparison:

```powershell
py -3.14 scripts/kimi_strategy_experiment.py execute --state-dir scratch/kimi-experiment-owner-v1 --owner-execute
py -3.14 scripts/kimi_strategy_experiment.py score --episodes scratch/kimi-development-pilot.json --state-dir scratch/kimi-experiment-owner-v1
```

The owner may replace environment-key use with `--credential-file` pointing to an existing private, one-line key file. This file is read only after `--owner-execute` has passed all registration/ledger checks. The assistant never reads or invokes this credential path. No signup, top-up, account query, or live configuration is needed for preparation.

General proxy environment variables are ignored. If the owner needs their existing credential-free HTTP(S) proxy in `KIMI_HTTPS_PROXY`, add `--use-kimi-proxy` explicitly. The destination remains the fixed official `https://api.moonshot.ai/v1/chat/completions`; redirects and configurable model endpoints are refused. The runner never reads the live bot's environment file, settings, models list, client, or budget logs.

## USD 10 cumulative ceiling

The same research ledger must be retained for every resume of this authorized experiment. Do not delete it or create another paid experiment to reset the owner's cumulative ceiling. The runner has no reset or retry command. An OS process lock covers the entire call sequence; reservations and replies use flushed, atomic file replacements. Separate fixture execution also requires a process lock.

Owner execution additionally locks one fixed grant at
`~/.bitpin-kimi-evidence-grant-20261011/owner-budget.json`. Every paid directory
shares this USD 10 authorization. Changing `--state-dir` cannot reset it or retry
an already reserved input/repeat. The shared charge is durable before the local
ledger write and HTTP request; a crash between them can waste an allowance but
cannot make a charge disappear. Offline prepare/score/fixtures never access this
owner profile path. Use one owner account on one machine for this pilot; do not
delete the grant or switch machines/accounts to acquire another allowance.

Before each request, the runner reserves integer micro-USD using:

```text
input_token_upper_bound = UTF-8 bytes of the entire serialized payload + 4096 framing tokens
reservation_USD = (input_token_upper_bound * 6 + 8192 * 15) / 1,000,000
```

The input allowance is deliberately conservative: one token per payload byte plus framing, no cached-input discount, and both $3/M uncached input and $3/M five-minute cache writes. At the published rates, cache reads/writes/uncached input are mutually exclusive, so this over-reserves input rather than relying on a discount. Output reserves the full 8192 tokens, including reasoning. For illustration, 20,000 reserved input tokens cost at most $0.24288 per call under this formula; 36 such calls reserve $8.74368. Actual registered totals are calculated from each exact payload, and preparation rejects a whole plan exceeding $10.

Reservations are never refunded automatically, including successful replies, missing usage, failures, timeouts, interrupts, or lost responses. The usage-based cost stored with a successful response is a conservative estimate, not a reconciled invoice. Unknown usage remains explicitly unknown. A pending interrupted call, malformed/truncated reply, tool call, refusal, or transport failure halts the experiment; resuming cannot silently repeat it. Reconcile any unknown bill with the provider outside the runner before considering a separately reviewed experiment. Do not treat a timeout as a free call.

Published token charges and this bound were verified on 2026-10-11. Check the linked official rates before a later owner-run experiment; if higher, stop until the fixed reservation policy is reviewed. The runner pins cache TTL `5m`; it has no `1h` override.

The prepared measured pilot has 16 distinct development days, 34 registered
calls including one stability pair, and a conservative total reservation of
**$6.532950**. Its first pair reserves at most **$0.384426**. No calls have run.
The measured preparation rotates available BTC/ETH/SOL cases at fixed 30-day
intervals and includes the selected past-fitted technical probabilities,
calibration weight and baseline. The additional macro/flow/whale/news coverage
is explicitly absent. Both arms receive that packet; the raw-history arm also
receives the 60 bars. This tests whether additional raw history helps beyond
the same structured evidence; it is not a replay of today's entire live prompt.

## Reply validation and private artifacts

K3 always thinks. The runner uses documented `low`, `high`, or `max` reasoning effort, `max_completion_tokens=8192`, no tools, no streaming, and a strict JSON Schema. It omits K3's fixed sampling parameters; temperature zero is not a supported assumption. It parses final `message.content` only. A second local validator requires the exact horizon/schema, finite down/flat/up probabilities in [0,1] summing to one, a nullable weight in [0,1], a valid hypothetical decision, bounded rationale, and known evidence IDs.

`manifest.json` contains the frozen registration. `ledger.json` contains durable reservations, timestamps, request-bound successful replies, complete bounded assistant content/reasoning, usage, finish reason, billing status, and hashes. Known key/Bearer patterns and the actual owner credential are redacted. Headers, credential files, and exception/server error bodies are never logged. Response and artifact sizes are bounded. Keep these artifacts private under ignored `scratch/`; no credentials belong there.

The optional offline `--fixture-responses FILE` mode accepts only a JSON list of envelopes with exactly `request_hash` and `response`. Every envelope must match a registered request signature; arbitrary supplied replies are refused. It is mutually exclusive with owner flags/credential options and permanently labels that ledger as a fixture. The unit tests provide synthetic completions; they are software validation only.

Scoring is free and bound to the exact registered episode inputs. It reports valid arm coverage, paired cases, unique calendar days, multiclass Brier score, log loss, accuracy, the mean paired Brier difference, per-day paired differences, and repeat probability disagreement. Future/unmatured labels are excluded. Small-pilot confidence intervals remain unavailable; repeats and bootstrap resamples cannot turn a few market days into adequate evidence. A real pilot remains `inconclusive`/`research_only`, with no profitability or promotion claim.

Official references:

- [Kimi K3 request contract](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart)
- [Structured response format](https://platform.kimi.ai/docs/guide/response_format)
- [Context caching policy and five-minute pricing](https://platform.kimi.ai/docs/guide/context-caching)
- [K3 published input/output pricing](https://forum.moonshot.ai/t/kimi-k3-is-here-our-most-capable-model/480)
