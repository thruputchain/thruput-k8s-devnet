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

Without the script, create the key Secrets first, then apply an overlay. Delete the finished
`thruput-smoke` Job first, because a Job cannot be changed in place:

```bash
kubectl create namespace thruput
python3 k8s/base/scripts/keygen.py manifests | kubectl apply -f -
kubectl -n thruput delete job thruput-smoke --ignore-not-found
kubectl apply -k k8s/overlays/single-pod
```

## Production clusters

The `production` overlay runs three pods (`core`, `indexer`, `edge`). Preferred
anti-affinity spreads them across nodes when capacity allows. It sets
`DEV_ACCOUNTS=0`, so genesis funds no public dev accounts.

```bash
./scripts/k3s-devnet.sh up --overlay production --keys generate \
  --storage-class fast --volume-size 100Gi \
  --ingress rpc.example.com,faucet.example.com --ingress-class traefik \
  --cluster-issuer letsencrypt --monitoring
```

Choose a storage class available on your cluster. Storage defaults to the
cluster's default class and `20Gi`. An existing StatefulSet cannot change its
`volumeClaimTemplates`: run `down` first, then `up` with the new options.
Existing volumes keep their old class and size. Provision or migrate volumes
separately when changing storage for an existing chain.

`--keys generate` (the default) runs a Job that creates random identities only
when their Secrets are missing. `--keys byo` requires both Secrets to exist.
There are no checked-in test keys.

| Secret | Fields in `stringData` |
| --- | --- |
| `thruput-validator-key` | `VALIDATOR_SECRET_KEY`: hex Ed25519 seed + public key (64 bytes); `VALIDATOR_PUBLIC_KEY`: base58 public key |
| `thruput-faucet-key` | `GROUNDHOG_FAUCET_SECRET_KEY`: hex Ed25519 seed (32 bytes); `GROUNDHOG_FAUCET_PUBLIC_KEY`: base58 public key |

To create both locally:

```bash
kubectl create namespace thruput
python3 k8s/base/scripts/keygen.py manifests | kubectl apply -f -
./scripts/k3s-devnet.sh up --overlay production --keys byo
```

Keep custom key Secrets labelled `thruput.network/keys: "true"`. Plain `down`
keeps keys and chain volumes. `down --purge` deletes the namespace, including
keys and volumes. Keep the same faucet key when resuming an existing chain.

Ingress needs an installed controller and cert-manager. Point both DNS names
at the controller and supply an existing ClusterIssuer with `--cluster-issuer`.
The wrapper prints HTTPS endpoints and switches the public Services to
ClusterIP. Use `--public-rpc-url https://rpc.example.com` when the faucet should
advertise that address; it must also be reachable from inside the cluster.

`--monitoring` needs the Prometheus Operator ServiceMonitor CRD. Configure
Prometheus to select these ServiceMonitors and label its namespace:

```bash
kubectl label namespace <prometheus-namespace> thruput.network/metrics-scraper=true
```

Production NetworkPolicies deny other inbound traffic, allow traffic between
pods in `thruput`, expose RPC and faucet, and allow metrics from labelled
scraper namespaces. Install a CNI that enforces NetworkPolicy. The namespace
enforces Pod Security `restricted`.

Each StatefulSet has a PodDisruptionBudget that keeps its single replica
available. Drains stop on these budgets. For maintenance, run `down`, drain,
then `up` with the same options. Use `restart` to restart every validator pod
together while keeping the chain.

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
| `production` | `core`, `indexer`, `edge`, spread by preferred anti-affinity |

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
- Keys live in `thruput-validator-key` and `thruput-faucet-key` Secrets.
  The wrapper generates missing keys by default. Use `--keys byo` for existing
  identities; see Production clusters above for the formats.
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

CI renders every overlay and runs the full smoke on a k3d cluster. A kind job
checks production with generated and supplied keys, TLS ingress, and enforced
NetworkPolicy.

## License

Apache-2.0. See [LICENSE](LICENSE).
