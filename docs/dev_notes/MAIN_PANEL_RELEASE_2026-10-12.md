# Main release and research panel

The owner requested integration into `main`, deployment to the running bot and
an update to its management panel. This release adds the authenticated
`/research` view to the existing bilingual panel. It preserves the current
live allocator and configuration; new evidence weights remain experimental.

## Panel behavior

- The sidebar opens **Research & data / پژوهش و داده‌ها**. IRT/USDT and
  holdout/development selectors show the corresponding evaluation results.
- Public-source freshness and coverage accompany Bitpin daily pressure,
  DefiLlama indicators, BTC custody observations and personal ETH balances.
  Missing history and partial daily coverage remain unknown.
- Group caps are research hypotheses. Active prospective influence is zero;
  incomplete source observations cannot become inferred trading signals.
- Evaluation cards distinguish price-only reconstructed tests from a full
  evidence/Kimi experiment. Costed compulsory daily reopening is a simulated
  comparator, not the running bot's account return. Recorded Kimi has zero
  evaluable cases, so its accuracy is unknown. Fresh Kimi remains `not_run`.
- The root helper accepts only the argument-free `research` command and reads
  fixed public snapshots plus the packaged sanitized aggregate. It does not
  read credentials, account state or invoke any network/model client.
- Existing authentication and CSP apply. The packaged Vazirmatn font is served
  from a fixed same-origin route; the view works in Persian RTL and English.

## Integration and rollout procedure

1. Review the panel change independently and resolve actionable findings.
2. Run meaningful panel regressions and the complete compatibility suites.
3. Visually inspect the real authenticated handler with a synthetic local
   session and the public server snapshots on desktop and mobile.
4. Fast-forward `main` from the reviewed feature branch and publish without
   rewriting history. The original `origin/main` is an ancestor of the release.
5. Test the exact public code tree on `hk` in isolated staging. Verify all
   packaged source hashes and existing configuration compatibility offline.
6. Install a code-only replacement under `/opt/bitpin-bot`, retain the prior
   tree, drain panel-helper operations and restart the previously active bot,
   notifier, panel and helper socket. Roll back if service health fails.
7. Verify installed hashes and public helper output. Preserve the flow process,
   all shadow timers, state, settings, confirmation and unrelated services.

The generic updater and key-loading CLI checks are intentionally unnecessary
for this code-only rollout. No new Kimi experiment calls, authenticated
exchange actions, settings writes or experimental live-weight activation are
part of this release. The exact installed commit is recorded by
`/opt/bitpin-bot/DEPLOYED_REVISION` and its deployment manifest.

The prior evidence release's measured results and limitations remain in
`EVIDENCE_STRATEGY_DELIVERY.md`; panel visibility does not establish additional
predictive performance. Promotion requires new causal observations and a
separately assessed after-cost comparison.

Independent review reproduced and resolved two presentation defects: missing
wallet rows incorrectly inherited a fresh source badge, and stablecoin units
were truncated. The final review found no remaining actionable findings in
authentication, safe public reads, schema projection or financial references.
Focused panel/helper/i18n verification passed 299 tests on Python 3.7 and 3.14
with 17 Windows/platform skips each. Complete final suites and installed health
are checked against the exact release tree during the rollout.
