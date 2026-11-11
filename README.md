# Thruput k8s devnet

Run a complete single-validator [Thruput](https://github.com/thruputchain) devnet
on k3s or any Kubernetes cluster. The deployment includes every hogd daemon, the coordinator,
the JSON-RPC gateway, and the faucet. It mints genesis, funds the dev accounts
and the faucet wallet, and runs a smoke Job that proves the chain works. The
manifests are plain Kustomize, so `kubectl apply -k` is all a cluster needs.

## Quick start

On Linux with k3s installed (`curl -sfL https://get.k3s.io | sh -`):

```bash
./scripts/k3s-devnet.sh up                   # all daemons in one pod
# or: ./scripts/k3s-devnet.sh up --overlay split   (one pod per daemon)
```

Each pod owns its chain volume, so switching topology needs `down --purge`
first.

On a laptop, create a local k3d cluster first. It pulls the validator image
into Docker once, loads it into every node, and switches kubectl to the
cluster, including when it already exists:

```bash
./scripts/k3s-devnet.sh local-cluster --agents 2   # three k3s nodes
./scripts/k3s-devnet.sh up --overlay multi-node    # after labelling nodes, below
```

`up` waits for the validator and the smoke Job, then prints the endpoints. k3s
ServiceLB publishes the gateway (`18545` RPC, `19646` readiness) and the faucet
(`18085`) on every node. The k3d cluster maps those ports to localhost.

The other commands are `smoke`, `status`, `restart`, `down`, and
`down --purge`. Plain `down` keeps the chain volumes, so the next `up` with the
same overlay resumes the same chain. `--purge` deletes the namespace, and a
new chain starts on the next `up`. `up` refuses to run while workloads or
volumes from a different topology remain, or when an overlay moves a daemon
to a different pod. The `thruput-topology` ConfigMap records the placement.

Without the script, apply an overlay directly. Delete the finished
`thruput-smoke` Job first, because a Job cannot be changed in place:

```bash
kubectl -n thruput delete job thruput-smoke --ignore-not-found
kubectl apply -k k8s/overlays/single-pod
```

## What runs

| Daemon | Ports | State |
| --- | --- | --- |
| `hog-mempool` | 8070-8076 | none |
| `hog-db` | 8080-8084 | `/data/hogdb` |
| `hog-event` | 8090-8092 | `/data/event` (mode `0700`) |
| `hog-exec` | 8040-8042 | none |
| `hog-auth` | 8050-8051 | none |
| `hog-indexer` | 8061-8062 | `/data/indexer` |
| `hog-coordinator` | 7888, 50000, 3333-3334, 9101, 8088 | `/data/coordinator` |
| `hog-gateway` | 18545, 19646 | none |
| `hog-faucet` | 18085 | none |

Everything lives under `k8s/`. Every daemon has a Service with its own name. Daemons always address each
other through these Services, so the wiring is the same wherever a daemon
runs. The Services publish addresses before readiness, because pods that host
several daemons only become Ready once all of them are up.

Startup is ordered in the containers themselves, not with init containers, so
any grouping works:

1. `prepare-data` (init, every pod) creates the state directories.
2. `genesis` (init, coordinator pod) mints genesis once. It runs
   `hog-hosted-bootstrap generate` to fund `DEV_ACCOUNTS` accounts and the
   faucet wallet, then writes `genesis.json` and `bootstrap/metadata.json`. A
   later start reuses them, and refuses to start if the faucet key changed.
3. The hogd daemons wait for their upstream ports (`WAIT_FOR_PORTS`), then
   register with the mempool.
4. The coordinator waits for the mempool, commits genesis, and writes the
   runtime manifest. Its `hog-bootstrap` sidecar serves `metadata.json` and
   `runtime-manifest.json` read-only on port `8088`.
5. The gateway and faucet read the committed genesis hash from port `8088`.
   The faucet renders its config, including the funded wallet, before it
   starts.
6. The `thruput-smoke` Job checks that every dev account and the faucet hold
   their minted balance, then airdrops to a new wallet.

## Topologies

An overlay lists its pods. Each pod is a StatefulSet built from `pod/`, and a
`thruput.network/hosts-<daemon>: "true"` label in its kustomization picks which
daemons run in it. Each daemon is a Component in `components/` that adds its
container to the pod carrying that label. Any grouping is valid if every daemon
is hosted exactly once.

| Overlay | Pods |
| --- | --- |
| `single-pod` | `validator`: everything |
| `split` | one pod per daemon, placed by the scheduler |
| `multi-node` | `core` (hogd and coordinator), `indexer`, `edge` (gateway and faucet), pinned by node label |

`multi-node` places pods by node label:

```bash
kubectl label node <a> thruput.network/node-role=core
kubectl label node <b> thruput.network/node-role=indexer
kubectl label node <c> thruput.network/node-role=edge
```

For another placement, copy an overlay and edit the host labels under
`pods/*/kustomization.yaml`. Add a `nodeSelector` or affinity patch the same
way `multi-node` does. Keep `../../components/release` last in `components`,
so it sets the image on every container added before it.

## Configuration

- `base/settings.env` sets the network identity, minting amounts
  (`DEV_ACCOUNTS`, `DEV_ACCOUNT_TOKENS`, `FAUCET_TOKENS`), the faucet airdrop
  amount, and `DB_RAM_BYTES`. Override it with a `configMapGenerator` entry
  that uses `behavior: merge` in your overlay. Minting values apply only when
  genesis is first minted.
- `PUBLIC_RPC_URL` is the RPC URL the faucet both advertises and submits
  through, so it must be reachable from inside the cluster. The default
  in-cluster URL works for the smoke Job. Set it to a node or load-balancer
  address (`up --public-rpc-url`) when outside clients read the faucet
  descriptor.
- `base/faucet-key.env` and `base/chuckd.json` hold deterministic,
  publicly known test keys. Replace both with a `secretGenerator` in your
  overlay for any cluster that untrusted people can reach.
- `components/release` pins the `ghcr.io/thruputchain/thruput-validator`
  release; `scripts/set-image.py 0.1.33` moves it. `up --image NAME:TAG` or
  `NAME@sha256:DIGEST` overrides it without editing tracked files. For a
  private mirror, set `THRUPUT_REGISTRY_USER` and `THRUPUT_REGISTRY_TOKEN` and
  `up` creates the `thruput-registry` pull secret.
- Each pod requests a `20Gi` `ReadWriteOnce` volume from the default storage
  class (`local-path` on k3s), which keeps that pod on its first node.

## Restarts

Today the validator daemons are one restart domain.
Kubernetes restarts a single failed container or pod, and that recovers only
for some services. Measured on `0.1.32` by replacing one pod at a time:

| Replaced alone | Recovers |
| --- | --- |
| `hog-indexer`, `hog-gateway`, `hog-faucet` | yes |
| `hog-mempool`, `hog-db`, `hog-event`, `hog-exec`, `hog-auth`, `hog-coordinator` | no, block production stops |

Restarting a single container also fails: a restarted `hog-exec` registers as
an extra executor, the mempool restarts, and the other daemons do not
re-register.

Recovery is to restart every validator pod together. The chain resumes from
its volumes:

```bash
./scripts/k3s-devnet.sh restart
```

Restarting services one at a time is planned.

## Development

```bash
python3 -m unittest discover -s tests   # manifest contract tests; needs kubectl for rendering
```

CI renders every overlay and runs the full smoke on a k3d cluster.

## License

Apache-2.0. See [LICENSE](LICENSE).
