#!/bin/bash
# Start chuckd. The first start commits genesis and writes the runtime
# manifest; later starts resume from the stored chain.
set -euo pipefail

state=${COORDINATOR_STATE:-/data/coordinator}
manifest=$state/bootstrap/runtime-manifest.json

args=(
  /usr/bin/chuckd
  --config /etc/groundhog/chuckd/chuckd.json
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
