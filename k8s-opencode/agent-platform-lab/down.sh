#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
CONF="${LAB_ENV:-$ROOT/infra/lab.env}"
if [[ ! -f "$CONF" ]]; then
  CONF="$ROOT/infra/lab.env.example"
fi
set -a; source "$CONF"; set +a

if pgrep -f "uvicorn app_chapeau.main:app" >/dev/null 2>&1; then
  echo "Stopping App Chapeau Web Console..."
  pkill -f "uvicorn app_chapeau.main:app" 2>/dev/null || true
fi

for ROLE in worker server; do
  NAME="$LAB_NAME-$ROLE"
  if sudo virsh dominfo "$NAME" >/dev/null 2>&1; then
    echo "Destroying and undefining VM $NAME..."
    sudo virsh destroy "$NAME" >/dev/null 2>&1 || true
    sudo virsh undefine "$NAME" --nvram >/dev/null 2>&1 || sudo virsh undefine "$NAME" >/dev/null 2>&1 || true
  fi
done

if sudo virsh net-info "$NETWORK" >/dev/null 2>&1; then
  echo "Stopping and undefining libvirt network $NETWORK..."
  sudo virsh net-destroy "$NETWORK" >/dev/null 2>&1 || true
  sudo virsh net-undefine "$NETWORK" >/dev/null 2>&1 || true
fi

if [[ "${1:-}" == "--keep-image" && -f "$WORKDIR/ubuntu-base.img" ]]; then
  echo "Cleaning $ROOT/.private (keeping cached $WORKDIR/ubuntu-base.img)..."
  rm -rf "$ROOT/.private/ssh" "$ROOT/.private/helm" "$ROOT/.private/kubeconfig" \
         "$WORKDIR/$LAB_NAME-server"* "$WORKDIR/$LAB_NAME-worker"* "$WORKDIR/network.xml"
else
  echo "Removing $ROOT/.private (ephemeral SSH keys, kubeconfig, Helm cache, VM disks)..."
  rm -rf "$ROOT/.private"
fi

rm -f "$ROOT/infra/lab.env"
echo "Clean complete."
