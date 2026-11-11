#!/bin/bash
# Wait for the coordinator to commit genesis, then start the gateway against
# the per-daemon Services with the committed chain identity.
set -euo pipefail

genesis_hash=$(python3 - "$BOOTSTRAP_URL/runtime-manifest.json" <<'EOF'
import json, sys, time, urllib.request

url = sys.argv[1]
for attempt in range(600):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            manifest = json.load(response)
        commit = manifest["hostedBootstrap"]["commit"]
        if commit["height"] == 1:
            print(commit["valueHash"])
            sys.exit(0)
    except Exception as error:  # Coordinator still starting.
        if attempt % 30 == 0:
            print(f"waiting for {url}: {error}", file=sys.stderr)
    time.sleep(2)
sys.exit(f"timed out waiting for committed genesis at {url}")
EOF
)
echo "gateway serving genesis $genesis_hash"

# The gateway takes socket addresses, not host names. Service IPs are stable.
service_ip() {
  local ip rest
  read -r ip rest < <(getent hosts "$1") || true
  [[ -n "${ip:-}" ]] || { echo "cannot resolve Service $1" >&2; exit 1; }
  printf '%s' "$ip"
}
scheduler=$(service_ip hog-mempool)
indexer=$(service_ip hog-indexer)
db_gateway=$(service_ip hog-db)
view_gateway=$(service_ip hog-exec)

exec /usr/bin/hog-gateway \
  --listen 0.0.0.0:18545 \
  --observability-listen 0.0.0.0:19646 \
  --network-id "$NETWORK_ID" \
  --network-hash "$NETWORK_HASH" \
  --genesis-hash "$genesis_hash" \
  --faucet-url "$FAUCET_URL" \
  --health-required-backends scheduler,indexer,db_gateway,view_gateway \
  --scheduler "$scheduler:8075" \
  --indexer "$indexer:8061" \
  --db-gateway "$db_gateway:8084" \
  --view-gateway "$view_gateway:8042"
