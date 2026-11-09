#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export KUBECONFIG="$ROOT/.private/kubeconfig"
NODE=$(kubectl -n agent-lab get pods -l app=opencode-sre -o jsonpath='{.items[0].spec.nodeName}')
[[ -n "$NODE" ]] || { echo 'Wake the agent first' >&2; exit 1; }
echo "Source node: $NODE"
"$ROOT/tests/hibernate.sh"
kubectl cordon "$NODE"
cleanup() { kubectl uncordon "$NODE"; }
trap cleanup EXIT
"$ROOT/tests/wake.sh"
NEW_NODE=$(kubectl -n agent-lab get pod -l app=opencode-sre -o jsonpath='{.items[0].spec.nodeName}')
[[ "$NEW_NODE" != "$NODE" ]] || { echo 'ERROR: pod did not change nodes' >&2; exit 1; }
echo "Migrated $NODE -> $NEW_NODE"
"$ROOT/tests/check-db.sh"
