#!/bin/bash
# Start chuckd. The first start commits genesis and writes the runtime
# manifest; later starts resume from the stored chain.
set -euo pipefail

state=${COORDINATOR_STATE:-/data/coordinator}
manifest=$state/bootstrap/runtime-manifest.json

# Validate before rendering; never print the supplied key material on failure.
config=$(python3 - <<'PY'
import os
from pathlib import Path
import re
import tempfile

secret = os.environ.get("VALIDATOR_SECRET_KEY", "")
public = os.environ.get("VALIDATOR_PUBLIC_KEY", "")
alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
if not re.fullmatch(r"[0-9a-f]{128}", secret):
    raise SystemExit("VALIDATOR_SECRET_KEY must be 128 lowercase hex characters (seed + public key)")
if not 32 <= len(public) <= 44 or any(c not in alphabet for c in public):
    raise SystemExit("VALIDATOR_PUBLIC_KEY must be base58 of a 32-byte public key")
number = 0
for c in public:
    number = number * 58 + alphabet.index(c)
decoded = b"\0" * (len(public) - len(public.lstrip("1"))) + number.to_bytes((number.bit_length() + 7) // 8, "big")
if len(decoded) != 32 or decoded.hex() != secret[64:]:
    raise SystemExit("VALIDATOR_PUBLIC_KEY must match the 32-byte public key in VALIDATOR_SECRET_KEY")
template = Path("/opt/thruput/scripts/chuckd.template.json").read_text()
with tempfile.NamedTemporaryFile(mode="w", prefix="chuckd-", suffix=".json", dir="/tmp", delete=False) as output:
    os.fchmod(output.fileno(), 0o600)
    output.write(template.replace("__VALIDATOR_SECRET_KEY__", secret).replace("__VALIDATOR_PUBLIC_KEY__", public))
    print(output.name)
PY
)

args=(
  /usr/bin/chuckd
  --config "$config"
  --metrics=:9101
  --stack
  sys coordinator
  --sched-addr __IP_OF_HOG_MEMPOOL__:8070
  --in-addr __CONTAINER_IP__:7888
  --out-addr __CONTAINER_IP__:50000
  --scp-handle-addr __CONTAINER_IP__:3333
  --scp-control-addr __CONTAINER_IP__:3334
  --scpd-path /usr/bin/scpd
  --storage-dir "$state/chuckd"
)

if [[ -s "$manifest" ]]; then
  echo "resuming coordinator from $manifest"
else
  echo "initializing coordinator from minted genesis"
  args+=(
    --genesis-file "$state/genesis.json"
    --hosted-bootstrap-metadata "$state/bootstrap/metadata.json"
    --hosted-bootstrap-scheduler-gateway __IP_OF_HOG_MEMPOOL__:8075
    --hosted-bootstrap-indexer __IP_OF_HOG_INDEXER__:8061
    --hosted-bootstrap-db-gateway __IP_OF_HOG_DB__:8084
    --hosted-bootstrap-view-gateway __IP_OF_HOG_EXEC__:8042
    --hosted-bootstrap-runtime-manifest-out "$manifest"
    --hosted-bootstrap-public-rpc-url "$PUBLIC_RPC_URL"
  )
fi

exec /bin/bash /opt/groundhog/with-container-ip.sh "${args[@]}"
