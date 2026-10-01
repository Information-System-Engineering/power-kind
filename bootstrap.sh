#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-power-kind}"
PROM_HOSTPORT="${PROM_HOSTPORT:-30090}"
KUBE_PROM_VERSION="${KUBE_PROM_VERSION:-82.0.0}"
ENABLE_FAKE_POWER="${ENABLE_FAKE_POWER:-true}"
ENABLE_PROCESS_EXPORTER="${ENABLE_PROCESS_EXPORTER:-true}"

KCTX="kind-${CLUSTER_NAME}"
k() { kubectl --context "$KCTX" "$@"; }

# Helm state stays in-repo so this never touches the user's ~/.helm.
export HELM_CACHE_HOME="${HERE}/.helm/cache"
export HELM_CONFIG_HOME="${HERE}/.helm/config"
export HELM_DATA_HOME="${HERE}/.helm/data"
mkdir -p "$HELM_CACHE_HOME" "$HELM_CONFIG_HOME" "$HELM_DATA_HOME"

for tool in docker kind kubectl helm; do
  command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required but not installed." >&2; exit 1; }
done

if lsof -nP -iTCP:"${PROM_HOSTPORT}" -sTCP:LISTEN >/dev/null 2>&1 &&
   ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "Host port ${PROM_HOSTPORT} is already bound." >&2
  docker ps --filter "publish=${PROM_HOSTPORT}" --format '  held by: {{.Names}} ({{.Image}})' >&2
  echo "Free it, or re-run with PROM_HOSTPORT=31090 $0" >&2
  exit 1
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "==> Cluster '${CLUSTER_NAME}' already exists; reusing it."
else
  echo "==> Creating kind cluster '${CLUSTER_NAME}' (Prometheus on host port ${PROM_HOSTPORT})"
  KIND_CFG="$(mktemp)"; trap 'rm -f "$KIND_CFG"' EXIT
  awk -v p="${PROM_HOSTPORT}" '
    /containerPort: 30090/ { seen=1 }
    seen && /hostPort:/     { sub(/hostPort:.*/, "hostPort: " p); seen=0 }
    { print }
  ' "${HERE}/kind.yml" > "$KIND_CFG"
  kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CFG"
fi

k wait --for=condition=Ready nodes --all --timeout=180s >/dev/null

# kind.yml sets these, but a reused cluster may predate a change to it, and a
# mislabelled node makes every nodeSelector silently wrong.
SYSTEM_NODES="$(k get nodes -l role=system -o name | wc -l | tr -d ' ')"
SUT_NODES="$(k get nodes -l role=sut -o name | wc -l | tr -d ' ')"
if [[ "$SYSTEM_NODES" -ne 1 || "$SUT_NODES" -lt 1 ]]; then
  echo "Node labels wrong: expected 1 role=system and >=1 role=sut;" >&2
  echo "got system=${SYSTEM_NODES} sut=${SUT_NODES}." >&2
  k get nodes --show-labels >&2
  exit 1
fi
echo "==> Nodes ready: system=${SYSTEM_NODES} sut=${SUT_NODES}"

echo "==> Installing kube-prometheus-stack ${KUBE_PROM_VERSION}"
k create namespace monitoring --dry-run=client -o yaml | k apply -f -
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --kube-context "$KCTX" \
  --namespace monitoring \
  --version "${KUBE_PROM_VERSION}" \
  --values "${HERE}/values/kube-prometheus-stack.yaml" \
  --values "${HERE}/values/kube-prometheus-stack.kind.yaml" \
  --wait --timeout 10m

if [[ "$ENABLE_FAKE_POWER" == "true" ]]; then
  echo "==> Installing fake-power (SYNTHETIC power series — NOT a measurement)"
  k create configmap fake-power -n monitoring \
    --from-file="${HERE}/fake-power/fake_power_exporter.py" \
    --dry-run=client -o yaml | k apply -f -
  k apply -f "${HERE}/fake-power/manifest.yaml"
  k rollout status deploy/fake-power -n monitoring --timeout=3m
else
  echo "==> fake-power disabled; every power query returns no series."
fi

if [[ "$ENABLE_PROCESS_EXPORTER" == "true" ]]; then
  echo "==> Installing process-exporter"
  k create configmap process-exporter -n monitoring \
    --from-file=config.yml="${HERE}/process-exporter/config.yml" \
    --dry-run=client -o yaml | k apply -f -
  k apply -f "${HERE}/process-exporter/manifest.yaml"
  k rollout status ds/process-exporter -n monitoring --timeout=3m
else
  echo "==> process-exporter disabled."
fi

cat <<EOF

Cluster '${CLUSTER_NAME}' is ready.

  Prometheus   http://localhost:${PROM_HOSTPORT}
  In-cluster   http://kube-prometheus-stack-prometheus.monitoring:9090
  System node  $(k get nodes -l role=system -o jsonpath='{.items[*].metadata.name}')
  SUT nodes    $(k get nodes -l role=sut -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}')
EOF
