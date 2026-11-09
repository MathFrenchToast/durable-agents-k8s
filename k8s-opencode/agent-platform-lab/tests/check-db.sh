#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export KUBECONFIG="$ROOT/.private/kubeconfig"
POD=$(kubectl -n agent-lab get pod -l app=opencode-sre -o jsonpath='{.items[0].metadata.name}')
kubectl -n agent-lab exec "$POD" -- sh -c 'opencode debug paths db 2>/dev/null || opencode debug paths'
kubectl -n agent-lab exec "$POD" -- sh -c 'ls -lah /data; test -f /data/opencode.db && echo "DATABASE EXISTS" || echo "Database not initialized yet (run a session)"'
