#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export KUBECONFIG="$ROOT/.private/kubeconfig"
kubectl -n agent-lab scale deploy/opencode-sre --replicas=0
kubectl -n agent-lab wait --for=delete pod -l app=opencode-sre --timeout=180s || true
for _ in {1..60}; do
  STATE=$(kubectl -n longhorn-system get volumes.longhorn.io -o jsonpath='{.items[0].status.state}' 2>/dev/null || true)
  [[ "$STATE" == "detached" || -z "$STATE" ]] && break
  sleep 0.5
done
kubectl -n agent-lab get pods,pvc
