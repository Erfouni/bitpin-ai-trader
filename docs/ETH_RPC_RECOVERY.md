# ETH public RPC recovery — 3.14.1

The secondary gateway responded to mainnet chain identity, finalized heads and numeric block headers, but timed out
on a native-balance request at the same finalized height. Repeated collector failures therefore had no valid balance
quorum. A bounded boundary trace distinguished this from a malformed request, a stale block or an HTTP rate-limit
response. The public snapshots correctly withheld balances throughout the failure.

The replacement is the keyless public dRPC gateway, `https://eth.drpc.org`. Its official documentation publishes the
endpoint, demonstrates finalized block reads and describes numeric-block balance queries returning exact wei.
A bounded public probe checked all three catalog addresses against PublicNode at one common finalized block and
rechecked the block headers afterward; all exact balances and block identities agreed.

## Minimal repair

1. Use PublicNode plus dRPC for new collector observations, with each gateway's actual identity.
2. Retain the read-only method/address allowlist, two-second spacing, 20-request ceiling, persistent cooldowns,
   block-age limits, exact balance agreement, previous-anchor checks and final header checks.
3. Let the live reader accept exactly the current pair or the legacy PublicNode/Flashbots pair. Unknown pairs,
   three-source mixtures, failures, disagreements and stale evidence remain unavailable.
4. Preserve SQLite observations, receipt times and chain epochs. Verify the prior successful anchor with both
   current gateways; changing gateways alone neither fabricates history nor erases a chain warning.
5. Display the gateways present in the receipt in the personal report and authenticated research panel.
6. Test offline, review, install both the isolated collector and live reader, refresh the public receipt, then
   run the credential-free wallet activation preview as the bot service user.

No key, paid service, account request, transaction submission or model call is needed. dRPC routes requests through
multiple upstream providers/public nodes; agreement between two approved gateways is not proof of independent
underlying operators. Free public RPC access has limits and no uptime guarantee. Current ownership and whether
funds are personal remain unverified; balances are not trades or established directional signals.

The repair does not activate wallet settings. Once a valid fresh receipt exists, the owner uses the interactive
[wallet activation helper](LIVE_EVIDENCE_CONTEXT.md), completes `check` and `confirm-live`, and restarts through that helper.

## Primary sources

- [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/)
- [Flashbots read-method limits](https://docs.flashbots.net/flashbots-protect/ratelimiting)
- [dRPC public Ethereum endpoint](https://drpc.org/docs/ethereum-api)
- [dRPC finalized block reads](https://drpc.org/docs/ethereum-api/blocksinfo/eth_getBlockByNumber)
- [dRPC balance queries](https://drpc.org/docs/ethereum-api/accountinfo/eth_getBalance)
- [dRPC rate limits](https://drpc.org/docs/howitworks/ratelimiting)
- [dRPC upstream architecture](https://drpc.org/docs/howitworks/overview)
