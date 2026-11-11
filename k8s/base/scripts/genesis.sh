#!/bin/bash
# Init container: mint the chain's genesis once. Funds the dev accounts and the
# faucet wallet, then leaves genesis.json and bootstrap/metadata.json on the
# coordinator's volume. Later starts reuse them so the chain resumes.
set -euo pipefail

state=${COORDINATOR_STATE:-/data/coordinator}
genesis=$state/genesis.json
metadata=$state/bootstrap/metadata.json

mkdir -p "$state/bootstrap" "$state/chuckd"

if [[ -s "$genesis" && -s "$metadata" ]]; then
  recorded=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["faucet"]["publicKey"])' "$metadata")
  if [[ "$recorded" != "$GROUNDHOG_FAUCET_PUBLIC_KEY" ]]; then
    echo "existing genesis funds faucet $recorded, not $GROUNDHOG_FAUCET_PUBLIC_KEY;" \
      "delete the coordinator volume to mint a new chain" >&2
    exit 1
  fi
  echo "reusing minted genesis at $genesis"
  exit 0
fi

# A runtime manifest without the genesis that produced it cannot be resumed.
rm -f "$genesis" "$metadata" "$state/bootstrap/runtime-manifest.json"

/usr/bin/hog-hosted-bootstrap generate \
  --network-hash "$NETWORK_HASH" \
  --dev-accounts "$DEV_ACCOUNTS" \
  --dev-account-seed "$DEV_ACCOUNT_SEED" \
  --dev-account-tokens "$DEV_ACCOUNT_TOKENS" \
  --faucet-public-key "$GROUNDHOG_FAUCET_PUBLIC_KEY" \
  --faucet-tokens "$FAUCET_TOKENS" \
  --contract-artifacts-dir /usr/share/thruput/contracts \
  --genesis-out "$genesis.tmp" \
  --metadata-out "$metadata.tmp"
mv "$metadata.tmp" "$metadata"
mv "$genesis.tmp" "$genesis"
echo "minted genesis: $DEV_ACCOUNTS dev accounts at $DEV_ACCOUNT_TOKENS, faucet at $FAUCET_TOKENS"
