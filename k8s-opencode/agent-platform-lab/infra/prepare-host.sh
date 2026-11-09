#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
export PATH="$ROOT/.private/bin:$PATH"

NEED_APT=0
for cmd in virsh virt-install qemu-img curl jq ssh ssh-keygen; do
  command -v "$cmd" >/dev/null 2>&1 || NEED_APT=1
done
if ! command -v cloud-localds >/dev/null 2>&1 && ! command -v xorrisofs >/dev/null 2>&1 && ! command -v genisoimage >/dev/null 2>&1; then
  NEED_APT=1
fi

if [[ "$NEED_APT" -eq 1 ]]; then
  sudo apt-get update
  sudo apt-get install -y qemu-kvm libvirt-daemon-system libvirt-clients virtinst qemu-utils cloud-image-utils curl jq openssh-client acl
  sudo systemctl enable --now libvirtd || sudo systemctl enable --now virtqemud
  sudo usermod -aG libvirt,kvm "$USER"
fi

if ! command -v helm >/dev/null 2>&1; then
  echo "Installing Helm 3 locally into $ROOT/.private/bin/helm (no host pollution)..."
  mkdir -p "$ROOT/.private/bin"
  HELM_VER="v3.17.1"
  curl -fsSL "https://get.helm.sh/helm-${HELM_VER}-linux-amd64.tar.gz" | tar -xz -C "$ROOT/.private/bin" --strip-components=1 linux-amd64/helm
  chmod +x "$ROOT/.private/bin/helm"
fi

printf 'Host ready (all lab artifacts confined to %s/.private).\n' "$ROOT"
