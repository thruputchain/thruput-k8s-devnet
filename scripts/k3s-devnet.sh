#!/usr/bin/env bash
# Deploy a Thruput devnet onto k3s (or any Kubernetes) from k8s/.
# See README.md.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
k8s_root=$repo_root/k8s
namespace=thruput
work_dir=$repo_root/.devnet
part_of=app.kubernetes.io/part-of=thruput

usage() {
  cat <<'EOF'
usage: scripts/k3s-devnet.sh <command> [options]

commands:
  up [--overlay NAME|DIR] [--image NAME:TAG|NAME@sha256:DIGEST] [--public-rpc-url URL]
                     apply a topology, wait for the validator, run the smoke Job
  smoke              rerun the post-deploy smoke Job
  status             show pods, placement, and the chain tip
  restart            restart every validator pod together (keeps the chain)
  down [--purge]     delete the workloads; --purge also deletes the chain data
  local-cluster [--agents N] [--name NAME]
                     create a local k3d cluster and load the validator image

overlays: single-pod (default), split, multi-node, or a directory of your own.
image:    --image, else THRUPUT_IMAGE, else the tag in k8s/components/release.
registry: set THRUPUT_REGISTRY_USER and THRUPUT_REGISTRY_TOKEN to create the
          thruput-registry pull secret for a private mirror.
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

default_image() {
  if [[ -n "${THRUPUT_IMAGE:-}" ]]; then
    echo "$THRUPUT_IMAGE"
    return
  fi
  local tag
  tag=$(awk '$1 == "newTag:" {print $2}' "$k8s_root/components/release/kustomization.yaml")
  [[ -n "$tag" ]] || die "no newTag in k8s/components/release/kustomization.yaml"
  echo "ghcr.io/thruputchain/thruput-validator:$tag"
}

resolve_overlay() {
  local overlay=$1
  if [[ -d "$overlay" ]]; then
    (cd "$overlay" && pwd)
  elif [[ -d "$k8s_root/overlays/$overlay" ]]; then
    echo "$k8s_root/overlays/$overlay"
  else
    die "unknown overlay: $overlay"
  fi
}

# Wrap the chosen overlay so image and URL overrides never edit tracked files.
render_overlay() {
  local overlay=$1 image=$2 public_rpc_url=$3
  local name pin
  if [[ "$image" == *@sha256:* ]]; then
    name=${image%@*}
    pin="    digest: ${image#*@}"
  elif [[ "${image##*/}" == *:* ]]; then
    name=${image%:*}
    pin="    newTag: \"${image##*:}\""
  else
    die "--image must be NAME:TAG or NAME@sha256:DIGEST"
  fi
  rm -rf "$work_dir/overlay"
  mkdir -p "$work_dir/overlay"
  # Kustomize accepts only relative resource paths.
  overlay=$(python3 -c 'import os, sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' \
    "$overlay" "$work_dir/overlay")
  {
    echo "apiVersion: kustomize.config.k8s.io/v1beta1"
    echo "kind: Kustomization"
    echo "resources:"
    echo "  - $overlay"
    echo "images:"
    echo "  - name: ghcr.io/thruputchain/thruput-validator"
    echo "    newName: $name"
    echo "$pin"
    if [[ -n "$public_rpc_url" ]]; then
      echo "configMapGenerator:"
      echo "  - name: thruput-settings"
      echo "    behavior: merge"
      echo "    literals:"
      echo "      - PUBLIC_RPC_URL=$public_rpc_url"
    fi
  } >"$work_dir/overlay/kustomization.yaml"
  echo "$work_dir/overlay"
}

statefulsets_in() {
  awk '/^kind: StatefulSet$/ {s=1; next} s && /^  name: / {print $2; s=0}' "$1" | sort
}

# "<daemon>=<statefulset>" for every daemon, from the hosts labels.
placement_in() {
  awk '
    function flush() { for (i = 1; i <= n; i++) print daemons[i] "=" name; n = 0; name = "" }
    /^---$/ {flush(); sts = 0; next}
    /^kind: StatefulSet$/ {sts = 1; next}
    sts && /^  name: / {name = $2; next}
    sts && /^    thruput\.network\/hosts-hog-[a-z]+: "true"$/ {
      daemon = $1; sub(/^thruput\.network\/hosts-/, "", daemon); sub(/:$/, "", daemon)
      daemons[++n] = daemon
    }
    END {flush()}
  ' "$1" | sort
}

recorded_placement() {
  kubectl -n "$namespace" get configmap thruput-topology -o jsonpath='{.data.placement}' 2>/dev/null || true
}

deployed_statefulsets() {
  kubectl -n "$namespace" get statefulset -l "$part_of" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | sort
}

# Each pod owns its chain volume, so a different set of pods is a different
# chain. Refuse to mix it with workloads or volumes left from another one.
check_topology() {
  local wanted=$1 placement=$2 existing volumes expected recorded
  recorded=$(recorded_placement)
  # Moving a daemon between pods moves it off its stored state.
  if [[ -n "$recorded" && "$recorded" != "$placement" ]]; then
    die "this overlay places daemons differently from the deployed chain; run 'down --purge' first"
  fi
  # A subset is fine: an interrupted 'up' of this overlay may not have
  # created every pod or volume yet. Anything outside the overlay is not.
  existing=$(deployed_statefulsets | comm -23 - <(echo "$wanted"))
  if [[ -n "$existing" ]]; then
    die "a different topology is deployed (${existing//$'\n'/ }); run 'down --purge' first"
  fi
  expected=$(awk '{print "data-" $0 "-0"}' <<<"$wanted" | sort)
  volumes=$(kubectl -n "$namespace" get pvc -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null |
    grep '^data-' | sort | comm -23 - <(echo "$expected") || true)
  if [[ -n "$volumes" ]]; then
    die "chain volumes from another topology remain (${volumes//$'\n'/ }); run 'down --purge' first"
  fi
}

ensure_registry_secret() {
  if [[ -n "${THRUPUT_REGISTRY_USER:-}" && -n "${THRUPUT_REGISTRY_TOKEN:-}" ]]; then
    kubectl -n "$namespace" create secret docker-registry thruput-registry \
      --docker-server="${THRUPUT_REGISTRY_SERVER:-ghcr.io}" \
      --docker-username="$THRUPUT_REGISTRY_USER" \
      --docker-password="$THRUPUT_REGISTRY_TOKEN" \
      --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    echo "registry pull secret thruput-registry is set"
  fi
}

wait_validator() {
  local statefulset
  echo "waiting for validator pods..."
  for statefulset in $(deployed_statefulsets); do
    kubectl -n "$namespace" rollout status "statefulset/$statefulset" --timeout=600s
  done
}

wait_smoke() {
  echo "waiting for the smoke Job (minting, faucet airdrop)..."
  local complete failed
  for _ in $(seq 1 300); do
    complete=$(kubectl -n "$namespace" get job thruput-smoke -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}')
    failed=$(kubectl -n "$namespace" get job thruput-smoke -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}')
    if [[ "$complete" == True ]]; then
      kubectl -n "$namespace" logs job/thruput-smoke | tail -3
      return 0
    fi
    [[ "$failed" == True ]] && break
    sleep 2
  done
  kubectl -n "$namespace" logs job/thruput-smoke --tail=40 >&2 || true
  die "smoke Job did not complete"
}

endpoints() {
  cat <<EOF

Thruput devnet is up in namespace $namespace.
  JSON-RPC:  kubectl -n $namespace port-forward svc/hog-gateway 18545  (or the LoadBalancer address)
  Faucet:    kubectl -n $namespace port-forward svc/hog-faucet 18085
  Readiness: kubectl -n $namespace port-forward svc/hog-gateway 19646, then GET /readyz
On k3s the gateway and faucet Services are also published on every node at
ports 18545, 19646, and 18085 by the built-in ServiceLB.
EOF
}

cmd_up() {
  local overlay=single-pod image public_rpc_url=""
  image=$(default_image)
  while (($#)); do
    case $1 in
      --overlay) overlay=$2; shift 2 ;;
      --image) image=$2; shift 2 ;;
      --public-rpc-url) public_rpc_url=$2; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  need kubectl
  overlay=$(resolve_overlay "$overlay")
  local rendered manifest
  rendered=$(render_overlay "$overlay" "$image" "$public_rpc_url")
  # Kept aside until the apply succeeds, so 'smoke' keeps the last good one.
  manifest=$work_dir/rendered.next.yaml
  kubectl kustomize "$rendered" >"$manifest"
  if grep -E '^ +image: ' "$manifest" | grep -vqF "image: $image"; then
    die "the overlay sets an image other than $image; set it in the overlay instead"
  fi
  local placement
  placement=$(placement_in "$manifest")
  [[ -n "$placement" ]] || die "the overlay hosts no daemons"
  check_topology "$(statefulsets_in "$manifest")" "$placement"

  kubectl get namespace "$namespace" >/dev/null 2>&1 || kubectl create namespace "$namespace" >/dev/null
  ensure_registry_secret
  # Jobs are immutable; replace the previous smoke Job with this apply's.
  kubectl -n "$namespace" delete job thruput-smoke --ignore-not-found --wait=true >/dev/null
  kubectl apply -f "$manifest"
  # Not labelled part-of: a plain 'down' keeps it with the chain volumes.
  kubectl -n "$namespace" create configmap thruput-topology --from-literal=placement="$placement" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  mv "$manifest" "$work_dir/rendered.yaml"
  wait_validator
  wait_smoke
  endpoints
}

cmd_smoke() {
  need kubectl
  [[ -f "$work_dir/rendered.yaml" ]] || die "run up first; smoke reuses its rendered manifest"
  kubectl -n "$namespace" delete job thruput-smoke --ignore-not-found --wait=true >/dev/null
  python3 - "$work_dir/rendered.yaml" <<'EOF' | kubectl apply -f - >/dev/null
import sys
documents = open(sys.argv[1], encoding="utf-8").read().split("\n---\n")
print("\n---\n".join(d for d in documents if "\nkind: Job\n" in "\n" + d + "\n"))
EOF
  wait_smoke
}

cmd_status() {
  need kubectl
  kubectl -n "$namespace" get pods -o wide
  kubectl -n "$namespace" get svc hog-gateway hog-faucet
  local statefulset image
  statefulset=$(deployed_statefulsets | head -1)
  [[ -n "$statefulset" ]] || die "no validator is deployed"
  image=$(kubectl -n "$namespace" get statefulset "$statefulset" -o jsonpath='{.spec.template.spec.containers[0].image}')
  kubectl -n "$namespace" run "tip-$RANDOM" --rm -i --restart=Never --quiet --image="$image" \
    --override-type=json --overrides='[{"op":"add","path":"/spec/imagePullSecrets","value":[{"name":"thruput-registry"}]},{"op":"add","path":"/spec/securityContext","value":{"runAsNonRoot":true,"runAsUser":999,"runAsGroup":999,"seccompProfile":{"type":"RuntimeDefault"}}},{"op":"add","path":"/spec/containers/0/securityContext","value":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}}]' \
    --command -- python3 -c '
import json, urllib.request
request = urllib.request.Request("http://hog-gateway:18545", method="POST",
    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getChainTip", "params": []}).encode(),
    headers={"content-type": "application/json"})
print("chain tip:", json.load(urllib.request.urlopen(request, timeout=5))["result"])
' || echo "chain tip unavailable"
}

cmd_restart() {
  need kubectl
  [[ -n "$(deployed_statefulsets)" ]] || die "no validator is deployed"
  # The validator daemons are one restart domain today: restart them together.
  kubectl -n "$namespace" delete pod -l thruput.network/pod --wait=true
  wait_validator
}

cmd_down() {
  local purge=0
  case ${1:-} in
    "") ;;
    --purge) purge=1 ;;
    *) die "unknown option: $1" ;;
  esac
  need kubectl
  if ((purge)); then
    kubectl delete namespace "$namespace" --ignore-not-found --wait=true
    echo "deleted namespace $namespace and its chain data"
  else
    kubectl -n "$namespace" delete statefulset,job,service,configmap,secret -l "$part_of" --ignore-not-found
    echo "kept the chain volumes: 'up' with the same overlay resumes that chain;"
    echo "'down --purge' deletes it, which a different overlay requires"
  fi
}

cmd_local_cluster() {
  local agents=2 name=thruput
  while (($#)); do
    case $1 in
      --agents) agents=$2; shift 2 ;;
      --name) name=$2; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  need k3d
  need docker
  need kubectl
  local image platform archive
  image=$(default_image)
  case "$(docker info --format '{{.Architecture}}')" in
    x86_64 | amd64) platform=linux/amd64 ;;
    aarch64 | arm64) platform=linux/arm64 ;;
    *) die "unsupported Docker architecture" ;;
  esac
  # Prepare the image before touching the cluster so a failure leaves nothing
  # behind. A single-platform archive avoids missing-blob errors from
  # multi-platform image stores; it needs Docker 28 or later.
  docker image inspect "$image" >/dev/null 2>&1 || docker pull --platform "$platform" "$image"
  mkdir -p "$work_dir"
  archive=$work_dir/validator-image.tar
  docker save --platform "$platform" -o "$archive" "$image"
  if k3d cluster get "$name" >/dev/null 2>&1; then
    echo "reusing k3d cluster $name"
  else
    k3d cluster create "$name" --agents "$agents" --wait \
      -p "18545:18545@loadbalancer" -p "19646:19646@loadbalancer" -p "18085:18085@loadbalancer"
  fi
  # Point kubectl at this cluster on both paths, so the next 'up' lands here.
  k3d kubeconfig merge "$name" --kubeconfig-merge-default --kubeconfig-switch-context >/dev/null
  [[ "$(kubectl config current-context)" == "k3d-$name" ]] || die "kubectl is not using k3d-$name"
  k3d image import -c "$name" "$archive"
  rm -f "$archive"
  echo "k3d cluster $name has $image; RPC, readiness, and faucet map to localhost 18545, 19646, 18085"
  if [[ -z "${THRUPUT_IMAGE:-}" ]]; then
    return 0
  fi
  echo "run 'up' with THRUPUT_IMAGE=$image still set so it deploys this image"
}

command=${1:-}
[[ -n "$command" ]] || { usage; exit 2; }
shift
case $command in
  up) cmd_up "$@" ;;
  smoke) cmd_smoke ;;
  status) cmd_status ;;
  restart) cmd_restart ;;
  down) cmd_down "$@" ;;
  local-cluster) cmd_local_cluster "$@" ;;
  -h | --help | help) usage ;;
  *) usage; exit 2 ;;
esac
