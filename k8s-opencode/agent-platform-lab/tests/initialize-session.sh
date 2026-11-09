#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export KUBECONFIG="$ROOT/.private/kubeconfig"
: "${OPENCODE_MODEL:?Set OPENCODE_MODEL to a provider/model supported by your v2 installation}"
POD=$(kubectl -n agent-lab get pod -l app=opencode-sre -o jsonpath='{.items[0].metadata.name}')
# A short write-free prompt creates data in the OpenCode database, if provider credentials are valid.
kubectl -n agent-lab exec "$POD" -- opencode run --standalone --model "$OPENCODE_MODEL" 'Answer exactly: AGENTLAB-OK'
"$ROOT/tests/check-db.sh"
