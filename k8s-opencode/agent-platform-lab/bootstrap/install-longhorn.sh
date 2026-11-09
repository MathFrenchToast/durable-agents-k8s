#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
set -a; source "${LAB_ENV:-$ROOT/infra/lab.env}"; set +a
export PATH="$ROOT/.private/bin:$PATH"
export KUBECONFIG="$ROOT/.private/kubeconfig"
# Keep Helm cache/config/data inside .private/helm to avoid polluting $HOME
export HELM_CACHE_HOME="$ROOT/.private/helm/cache"
export HELM_CONFIG_HOME="$ROOT/.private/helm/config"
export HELM_DATA_HOME="$ROOT/.private/helm/data"

command -v helm >/dev/null || { echo 'Helm 3 must be installed (run ./infra/prepare-host.sh)'; exit 1; }
command -v kubectl >/dev/null || { echo 'kubectl must be installed on host'; exit 1; }

helm repo add longhorn https://charts.longhorn.io
helm repo update
helm upgrade --install longhorn longhorn/longhorn --namespace longhorn-system --create-namespace \
  --version "$LONGHORN_VERSION" \
  --set defaultSettings.defaultReplicaCount=2 \
  --set defaultSettings.defaultDataPath=/var/lib/longhorn \
  --wait --timeout 15m
kubectl -n longhorn-system get pods
kubectl apply -f "$ROOT/platform/storageclass.yaml"
