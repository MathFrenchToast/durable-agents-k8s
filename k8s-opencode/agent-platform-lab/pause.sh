#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# pause.sh — Arrête proprement 1 ou 2 VMs KVM sans supprimer les disques (.qcow2)
# Usage:
#   ./pause.sh                 # Hiberne le Pod et éteint les 2 VMs (-8 Go RAM)
#   ./pause.sh --worker-only   # Bascule le Pod sur server et éteint worker (-4 Go RAM)
# ==============================================================================

ROOT=$(cd "$(dirname "$0")" && pwd)
export KUBECONFIG="$ROOT/.private/kubeconfig"

WORKER_ONLY=false
if [[ "${1:-}" == "--worker-only" ]]; then
  WORKER_ONLY=true
fi

vm_state() {
  virsh -c qemu:///system domstate "$1" 2>/dev/null | tr -d '\r\n' || echo "missing"
}

wait_vm_off() {
  local vm="$1"
  for _ in $(seq 1 30); do
    if [[ "$(vm_state "$vm")" == "shut off" ]]; then
      echo "✓ VM $vm arrêtée (RAM libérée)."
      return 0
    fi
    sleep 2
  done
  echo "Forçage de l'arrêt de $vm (virsh destroy)..."
  virsh -c qemu:///system destroy "$vm" >/dev/null 2>&1 || true
}

if [[ "$WORKER_ONLY" == "true" ]]; then
  echo "==> Mode économie 1 VM : arrêt de agentlab-worker uniquement (-4 Go RAM)..."
  if [[ "$(vm_state agentlab-worker)" == "running" ]]; then
    if [[ -f "$KUBECONFIG" ]] && kubectl get nodes >/dev/null 2>&1; then
      POD_NODE=$(kubectl -n agent-lab get pods -l app=opencode-sre -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)
      if [[ "$POD_NODE" == "agentlab-worker" ]]; then
        echo "Le Pod opencode-sre est sur agentlab-worker -> migration vers agentlab-server avant extinction..."
        "$ROOT/tests/migrate.sh" || true
      fi
      kubectl cordon agentlab-worker >/dev/null 2>&1 || true
    fi
    virsh -c qemu:///system shutdown agentlab-worker >/dev/null
    wait_vm_off agentlab-worker
  else
    echo "agentlab-worker est déjà arrêtée."
  fi
  exit 0
fi

echo "==> Mise en veille complète du lab (arrêt des 2 VMs, -8 Go RAM, 0 perte de données)..."
if [[ "$(vm_state agentlab-server)" == "running" ]] && [[ -f "$KUBECONFIG" ]]; then
  if kubectl get nodes >/dev/null 2>&1; then
    echo "1/3 Détachement propre du Pod et du volume Longhorn (hibernate)..."
    "$ROOT/tests/hibernate.sh" || true
  fi
fi

if [[ "$(vm_state agentlab-worker)" == "running" ]]; then
  echo "2/3 Arrêt ACPI de agentlab-worker..."
  virsh -c qemu:///system shutdown agentlab-worker >/dev/null
  wait_vm_off agentlab-worker
fi

if [[ "$(vm_state agentlab-server)" == "running" ]]; then
  echo "3/3 Arrêt ACPI de agentlab-server..."
  virsh -c qemu:///system shutdown agentlab-server >/dev/null
  wait_vm_off agentlab-server
fi

echo "✓ Lab en veille (0 Mo RAM consommé). Relancez avec ./resume.sh ou ./start-ui.sh"
