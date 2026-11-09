#!/usr/bin/env bash
set -euo pipefail
BASE_DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$BASE_DIR/.." && pwd)
CONF="${LAB_ENV:-$BASE_DIR/lab.env}"
if [[ ! -f "$CONF" ]]; then
  cp "$BASE_DIR/lab.env.example" "$CONF"
  echo "Initialized $CONF from lab.env.example"
fi
set -a; source "$CONF"; set +a

# Generate ephemeral SSH keypair inside the lab directory (.private/ssh) if missing
mkdir -p "$(dirname "$SSH_PRIVKEY_FILE")" "$WORKDIR"
chmod 700 "$ROOT/.private" "$(dirname "$SSH_PRIVKEY_FILE")"
if [[ ! -f "$SSH_PRIVKEY_FILE" ]]; then
  echo "Generating ephemeral SSH keypair at $SSH_PRIVKEY_FILE..."
  ssh-keygen -t ed25519 -N "" -C "agentlab-ephemeral" -f "$SSH_PRIVKEY_FILE" >/dev/null
  chmod 600 "$SSH_PRIVKEY_FILE"
fi

# Grant libvirt-qemu traverse permission (+x) on ancestor directories so QEMU can read disk images inside the project tree
if id libvirt-qemu >/dev/null 2>&1 && command -v setfacl >/dev/null 2>&1; then
  DIR_CURSOR="$WORKDIR"
  while [[ "$DIR_CURSOR" != "/" && "$DIR_CURSOR" != "." ]]; do
    setfacl -m u:libvirt-qemu:x "$DIR_CURSOR" 2>/dev/null || true
    DIR_CURSOR=$(dirname "$DIR_CURSOR")
  done
fi

IMAGE="$WORKDIR/ubuntu-base.img"
if [[ ! -f "$IMAGE" ]]; then
  echo "Downloading Ubuntu LTS cloud image to $IMAGE..."
  curl -fL "$CLOUD_IMAGE_URL" -o "$IMAGE"
fi

if ! sudo virsh net-info "$NETWORK" >/dev/null 2>&1; then
  cat > "$WORKDIR/network.xml" <<EOF
<network>
  <name>$NETWORK</name>
  <forward mode='nat'/>
  <bridge name='$BRIDGE' stp='on' delay='0'/>
  <ip address='$SUBNET.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='$SUBNET.100' end='$SUBNET.199'/>
      <host mac='$SERVER_MAC' name='$LAB_NAME-server' ip='$SERVER_IP'/>
      <host mac='$WORKER_MAC' name='$LAB_NAME-worker' ip='$WORKER_IP'/>
    </dhcp>
  </ip>
</network>
EOF
  sudo virsh net-define "$WORKDIR/network.xml"
  sudo virsh net-autostart "$NETWORK"
  sudo virsh net-start "$NETWORK"
elif ! sudo virsh net-info "$NETWORK" | grep -q "Active:.*yes"; then
  sudo virsh net-start "$NETWORK"
fi

PUBKEY=$(cat "$SSH_PUBKEY_FILE")
for ROLE in server worker; do
  NAME="$LAB_NAME-$ROLE"
  IPVAR="${ROLE^^}_IP"; IP="${!IPVAR}"
  MACVAR="${ROLE^^}_MAC"; MAC="${!MACVAR}"
  if sudo virsh dominfo "$NAME" >/dev/null 2>&1; then echo "Exists: $NAME, skipping"; continue; fi
  ROOT_DISK="$WORKDIR/$NAME-root.qcow2"
  DATA_DISK="$WORKDIR/$NAME-data.qcow2"
  cp --reflink=auto "$IMAGE" "$ROOT_DISK"
  qemu-img resize "$ROOT_DISK" "${SYSTEM_DISK_GIB}G"
  qemu-img create -f qcow2 "$DATA_DISK" "${DATA_DISK_GIB}G"
  mkdir -p "$WORKDIR/$NAME"
  cat > "$WORKDIR/$NAME/meta-data" <<EOF
instance-id: $NAME
local-hostname: $NAME
EOF
  cat > "$WORKDIR/$NAME/user-data" <<EOF
#cloud-config
hostname: $NAME
manage_etc_hosts: true
users:
  - name: $VM_USER
    groups: [sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $PUBKEY
ssh_pwauth: false
package_update: true
packages:
  - open-iscsi
  - nfs-common
  - cryptsetup
  - util-linux
  - jq
runcmd:
  - [ sh, -c, 'systemctl enable --now iscsid' ]
  - [ sh, -c, 'while [ ! -b /dev/vdb ]; do sleep 1; done; blkid /dev/vdb || mkfs.ext4 -F /dev/vdb' ]
  - [ mkdir, -p, /var/lib/longhorn ]
  - [ sh, -c, 'grep -q " /var/lib/longhorn " /etc/fstab || echo "/dev/vdb /var/lib/longhorn ext4 defaults,noatime 0 2" >> /etc/fstab' ]
  - [ mount, /var/lib/longhorn ]
EOF
  cat > "$WORKDIR/$NAME/network-config" <<EOF
version: 2
ethernets:
  enp1s0:
    dhcp4: true
EOF
  sudo virt-install --connect qemu:///system --name "$NAME" \
    --memory "$VM_RAM_MIB" --vcpus "$VM_VCPUS" --cpu host-passthrough \
    --import --os-variant ubuntu24.04 --graphics none --noautoconsole \
    --disk "path=$ROOT_DISK,format=qcow2,bus=virtio" \
    --disk "path=$DATA_DISK,format=qcow2,bus=virtio" \
    --network "network=$NETWORK,mac=$MAC,model=virtio" \
    --cloud-init "user-data=$WORKDIR/$NAME/user-data,meta-data=$WORKDIR/$NAME/meta-data,network-config=$WORKDIR/$NAME/network-config"
  echo "Created $NAME at $IP"
done
printf '\nWait for cloud-init then: ssh -o UserKnownHostsFile=%s -i %s %s@%s sudo cloud-init status --wait\n' "$KNOWN_HOSTS_FILE" "$SSH_PRIVKEY_FILE" "$VM_USER" "$SERVER_IP"
