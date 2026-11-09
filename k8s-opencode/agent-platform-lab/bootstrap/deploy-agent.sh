#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export KUBECONFIG="$ROOT/.private/kubeconfig"
for f in namespace.yaml storageclass.yaml opencode-configmap.yaml pvc.yaml deployment.yaml; do
 kubectl apply -f "$ROOT/platform/$f"
done
kubectl -n agent-lab get pvc,deployment
cat <<'EOF'
To enable an LLM: kubectl -n agent-lab create secret generic opencode-provider --from-literal=anthropic-api-key='YOUR_KEY'
Do not store credentials in Git or shell history; prefer External Secrets in real environments.
EOF
