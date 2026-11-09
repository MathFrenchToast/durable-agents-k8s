#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# resume.sh — Rallume les VMs KVM arrêtées sans reconstruire le lab
# Usage:
#   ./resume.sh                # Rallume agentlab-server et agentlab-worker
#   ./resume.sh --server-only  # Rallume uniquement agentlab-server (mode 1 VM)
# ==============================================================================

ROOT=$(cd "$(dirname "$0")" && pwd)
export KUBECONFIG="$ROOT/.private/kubeconfig"
source "$ROOT/infra/lab.env"

SERVER_ONLY=false
if [[ "${1:-}" == "--server-only" ]]; then
  SERVER_ONLY=true
fi

# Garantit que libvirt-qemu peut traverser les dossiers jusqu'à .private/vms
fix_libvirt_acls() {
  local dir="$ROOT/.private/vms"
  while [[ "$dir" != "/" && "$dir" != "/home" ]]; do
    setfacl -m u:libvirt-qemu:x "$dir" 2>/dev/null || true
    dir=$(dirname "$dir")
  done
  setfacl -m u:libvirt-qemu:rw "$ROOT/.private/vms/"*.qcow2 2>/dev/null || true
  setfacl -m u:libvirt-qemu:r "$ROOT/.private/vms/"*.iso "$ROOT/.private/vms/"*.img 2>/dev/null || true
}

vm_state() {
  virsh -c qemu:///system domstate "$1" 2>/dev/null | tr -d '\r\n' || echo "missing"
}

fix_libvirt_acls

# S'assurer que le réseau libvirt est actif
if ! virsh -c qemu:///system net-info "$LIBVIRT_NET" 2>/dev/null | grep -q "Active:.*yes"; then
  echo "Activation du réseau libvirt $LIBVIRT_NET..."
  virsh -c qemu:///system net-start "$LIBVIRT_NET" >/dev/null 2>&1 || true
fi

if [[ "$(vm_state "$SERVER_NAME")" != "running" ]]; then
  echo "Démarrage de $SERVER_NAME..."
  virsh -c qemu:///system start "$SERVER_NAME"
fi

if [[ "$SERVER_ONLY" == "false" ]] && [[ "$(vm_state "$WORKER_NAME")" != "running" ]]; then
  echo "Démarrage de $WORKER_NAME..."
  virsh -c qemu:///system start "$WORKER_NAME"
fi

echo "Attente de la disponibilité de l'API Kubernetes (https://${SERVER_IP}:6443)..."
for _ in $(seq 1 45); do
  if kubectl get nodes >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

kubectl uncordon "$SERVER_NAME" >/dev/null 2>&1 || true
if [[ "$SERVER_ONLY" == "false" ]]; then
  for _ in $(seq 1 30); do
    if kubectl get node "$WORKER_NAME" 2>/dev/null | grep -q " Ready"; then
      kubectl uncordon "$WORKER_NAME" >/dev/null 2>&1 || true
      break
    fi
    sleep 2
  done
fi

echo "✓ État des nœuds K3s :"
kubectl get nodes -o wide
