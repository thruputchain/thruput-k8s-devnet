#!/bin/bash
# Post-deploy Job: prove the chain commits blocks, minting funded every
# genesis account, and a fresh wallet can be funded through the faucet.
set -euo pipefail

rpc_url=${PUBLIC_RPC_URL:?}
faucet_url=${FAUCET_URL:?}
gateway_observability=${GATEWAY_OBSERVABILITY_URL:-http://hog-gateway:19646}

fetch() {
  python3 -c 'import sys, urllib.request; print(urllib.request.urlopen(sys.argv[1], timeout=5).read().decode())' "$1"
}

wait_http() {
  local url=$1
  for _ in $(seq 1 300); do
    if fetch "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "timed out waiting for $url" >&2
  return 1
}

json_field() {
  python3 -c '
import json, sys
value = json.load(sys.stdin)
for key in sys.argv[1].split("."):
    value = value[key]
print(value)
' "$1"
}

wait_http "$gateway_observability/readyz"
wait_http "$faucet_url/health"
descriptor=$(fetch "$faucet_url/v1/network")
network_id=$(json_field networkId <<<"$descriptor")
network_hash=$(json_field networkHash <<<"$descriptor")
echo "network $network_id ($network_hash)"

work=$(mktemp -d)
export XDG_CONFIG_HOME=$work/config
mkdir -p "$XDG_CONFIG_HOME"
cat >"$work/thruput.toml" <<EOF
default_profile = "k8s"

[profile.k8s]
network = "$network_id"
network_hash = "$network_hash"
rpc_url = "$rpc_url"
faucet_url = "$faucet_url"
default_key = "smoke"

[profile.k8s.contracts]
EOF
cd "$work"
cli() { thruput --non-interactive --json "$@"; }

# Minting: every genesis-funded account must hold a balance on chain.
accounts=$(fetch "$BOOTSTRAP_URL/metadata.json" | python3 -c '
import json, sys
metadata = json.load(sys.stdin)
for account in metadata["bootstrapAccounts"] + [metadata["faucet"]]:
    print(account["role"], account["walletAddress"])
')
while read -r role address; do
  balance=$(cli balance --profile k8s "$address" | json_field data.rawBalance)
  if [[ ! "$balance" =~ ^[1-9][0-9]*$ ]]; then
    echo "$role $address has no minted balance ($balance)" >&2
    exit 1
  fi
  echo "minted: $role $address holds $balance"
done <<<"$accounts"

# Faucet: a fresh wallet gets funded through it.
cli keys generate smoke --scheme ed25519 >/dev/null
wallet=$(cli wallet create --profile k8s --key smoke --name smoke --yes | json_field data.wallet)
tx=$(cli airdrop --profile k8s smoke --yes | json_field data.txHash)
balance=$(cli balance --profile k8s smoke | json_field data.rawBalance)
if [[ ! "$balance" =~ ^[1-9][0-9]*$ ]]; then
  echo "wallet $wallet balance $balance after airdrop $tx" >&2
  exit 1
fi
echo "faucet funded $wallet with $balance in $tx"
echo "smoke passed"
