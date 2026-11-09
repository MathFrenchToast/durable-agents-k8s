#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export KUBECONFIG="$ROOT/.private/kubeconfig"
kubectl -n agent-lab scale deploy/opencode-sre --replicas=1
kubectl -n agent-lab rollout status deploy/opencode-sre --timeout=300s
kubectl -n agent-lab get pods -o wide
