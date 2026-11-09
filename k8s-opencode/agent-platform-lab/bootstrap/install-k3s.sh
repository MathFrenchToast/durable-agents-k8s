#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
CONF="${LAB_ENV:-$ROOT/infra/lab.env}"
set -a; source "$CONF"; set +a
KNOWN_HOSTS_FILE="${KNOWN_HOSTS_FILE:-$ROOT/.private/ssh/known_hosts}"
SSH=(ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN_HOSTS_FILE" -o ConnectTimeout=5 -i "$SSH_PRIVKEY_FILE")

for ip in "$SERVER_IP" "$WORKER_IP"; do
  echo "Waiting for SSH on $ip..."
  for _ in {1..60}; do
    if "${SSH[@]}" "$VM_USER@$ip" 'true' >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  echo "Waiting for cloud-init and /var/lib/longhorn on $ip..."
  "${SSH[@]}" "$VM_USER@$ip" 'sudo cloud-init status --wait && test -d /var/lib/longhorn && mountpoint -q /var/lib/longhorn'
done

# Token is generated on server; never commit it to Git.
"${SSH[@]}" "$VM_USER@$SERVER_IP" "curl -sfL https://get.k3s.io | sudo INSTALL_K3S_VERSION='$K3S_VERSION' sh -s - server --write-kubeconfig-mode 600 --tls-san '$SERVER_IP'"
TOKEN=$("${SSH[@]}" "$VM_USER@$SERVER_IP" 'sudo cat /var/lib/rancher/k3s/server/node-token')
"${SSH[@]}" "$VM_USER@$WORKER_IP" "curl -sfL https://get.k3s.io | sudo K3S_URL='https://$SERVER_IP:6443' K3S_TOKEN='$TOKEN' INSTALL_K3S_VERSION='$K3S_VERSION' sh -"
mkdir -p "$ROOT/.private"; chmod 700 "$ROOT/.private"
"${SSH[@]}" "$VM_USER@$SERVER_IP" 'sudo cat /etc/rancher/k3s/k3s.yaml' | sed "s/127.0.0.1/$SERVER_IP/g" > "$ROOT/.private/kubeconfig"
chmod 600 "$ROOT/.private/kubeconfig"
echo "KUBECONFIG=$ROOT/.private/kubeconfig"
export KUBECONFIG="$ROOT/.private/kubeconfig"
for _ in {1..60}; do
  READY_COUNT=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2 == "Ready" {count++} END {print count+0}')
  [[ "$READY_COUNT" -ge 2 ]] && break
  sleep 2
done
kubectl get nodes -o wide
