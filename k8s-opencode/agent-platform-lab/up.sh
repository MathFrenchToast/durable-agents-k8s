#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
KUBECONFIG_PATH="$ROOT/.private/kubeconfig"

# Check if the lab is already provisioned
SERVER_STATE=$(virsh -c qemu:///system domstate agentlab-server 2>/dev/null | tr -d '\r\n' || echo "missing")

if [[ "$SERVER_STATE" != "missing" && -f "$KUBECONFIG_PATH" ]]; then
  echo "==> Lab VMs already exist (agentlab-server state: $SERVER_STATE)."
  if [[ "$SERVER_STATE" != "running" ]]; then
    echo "==> Resuming paused/stopped VMs..."
    "$ROOT/resume.sh"
  fi
  export KUBECONFIG="$KUBECONFIG_PATH"
  echo "==> Ensuring base agent manifests are applied..."
  "$ROOT/bootstrap/deploy-agent.sh"
else
  echo "==> [1/6] Preparing host & local tools (Helm)..."
  "$ROOT/infra/prepare-host.sh"

  echo "==> [2/6] Provisioning ephemeral SSH keys, Ubuntu LTS image & VMs in $ROOT/.private..."
  "$ROOT/infra/provision.sh"

  echo "==> [3/6] Installing K3s cluster..."
  "$ROOT/bootstrap/install-k3s.sh"

  echo "==> [4/6] Installing Longhorn via Helm..."
  "$ROOT/bootstrap/install-longhorn.sh"

  echo "==> [5/6] Deploying OpenCode agent manifests..."
  "$ROOT/bootstrap/deploy-agent.sh"

  echo "==> [6/6] Waking agent Pod and checking persistent volume..."
  "$ROOT/tests/wake.sh"
  "$ROOT/tests/check-db.sh"
fi

echo "==> Starting (or restarting) App Chapeau Web Console..."
"$ROOT/start-ui.sh" --background
ACTIVE_PORT=$(cat "$ROOT/.private/app_chapeau.port" 2>/dev/null || echo "8000")

cat <<EOF

===================================================================
Lab & App Chapeau are UP! All artifacts are confined to: $ROOT/.private/
- Web Console (App Chapeau) : http://localhost:$ACTIVE_PORT
- UI Logs                   : $ROOT/.private/app_chapeau.log
- Ephemeral kubeconfig      : export KUBECONFIG="$ROOT/.private/kubeconfig"
- Pause VMs (-8 GiB RAM)    : $ROOT/pause.sh
- Clean up everything       : $ROOT/down.sh
===================================================================
EOF
