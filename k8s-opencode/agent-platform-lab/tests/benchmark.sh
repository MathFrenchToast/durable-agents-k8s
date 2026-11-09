#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
export KUBECONFIG="$ROOT/.private/kubeconfig"

now_ms() { date +%s%3N; }

wait_volume_state() {
  local target="$1"
  local state=""
  for _ in {1..120}; do
    state=$(kubectl -n longhorn-system get volumes.longhorn.io -o jsonpath='{.items[0].status.state}' 2>/dev/null || true)
    [[ "$state" == "$target" ]] && return 0
    sleep 0.5
  done
  echo "WARN: Volume state did not reach $target (current: $state)" >&2
}

measure_wake() {
  local label="$1"
  local t0=$(now_ms)
  kubectl -n agent-lab scale deploy/opencode-sre --replicas=1 >/dev/null

  # Wait for Volume attached
  wait_volume_state "attached"
  local t_attach=$(now_ms)

  # Wait for Pod Ready
  kubectl -n agent-lab rollout status deploy/opencode-sre --timeout=300s >/dev/null
  local t_ready=$(now_ms)

  local pod=$(kubectl -n agent-lab get pod -l app=opencode-sre -o jsonpath='{.items[0].metadata.name}')
  local node=$(kubectl -n agent-lab get pod "$pod" -o jsonpath='{.spec.nodeName}')

  # Measure first CLI invocation inside container
  kubectl -n agent-lab exec "$pod" -- opencode --version >/dev/null
  local t_cli=$(now_ms)

  printf "[%s] Node=%s | Attach=%d ms | PodReady=%d ms | FirstCLI=%d ms\n" \
    "$label" "$node" "$((t_attach - t0))" "$((t_ready - t0))" "$((t_cli - t0))"
}

measure_hibernate() {
  local label="$1"
  local t0=$(now_ms)
  kubectl -n agent-lab scale deploy/opencode-sre --replicas=0 >/dev/null
  kubectl -n agent-lab wait --for=delete pod -l app=opencode-sre --timeout=180s >/dev/null 2>&1 || true
  local t_pod_del=$(now_ms)

  wait_volume_state "detached"
  local t_detach=$(now_ms)

  printf "[%s] PodTerminated=%d ms | VolumeDetached=%d ms\n" \
    "$label" "$((t_pod_del - t0))" "$((t_detach - t0))"
}

echo "==================================================================="
echo " 1. BASELINE RESOURCE FOOTPRINT (HOST KVM + K3S + LONGHORN)"
echo "==================================================================="
echo "--- Host QEMU processes RSS ---"
ps -C qemu-system-x86_64 -o pid,%cpu,%mem,rss,args | awk 'NR==1 {print "PID\t%CPU\t%MEM\tRSS_MB\tVM"} NR>1 {for(i=1;i<=NF;i++) if($i ~ /^guest=/) vm=$i; printf "%s\t%s\t%s\t%.1f MB\t%s\n", $1, $2, $3, $4/1024, vm}'
echo "--- K3s Nodes (kubectl top nodes) ---"
kubectl top nodes || true
echo "--- Memory by Namespace (kubectl top pods) ---"
kubectl top pods -A --no-headers | awk '
  {
    mem=$4; gsub(/Mi/,"",mem); cpu=$3; gsub(/m/,"",cpu);
    ns_mem[$1]+=mem; ns_cpu[$1]+=cpu; ns_cnt[$1]++
  }
  END {
    for (n in ns_cnt) printf "Namespace %-18s : %2d pods | %4d mCPU | %5d MiB RAM\n", n, ns_cnt[n], ns_cpu[n], ns_mem[n]
  }' | sort

echo ""
echo "==================================================================="
echo " 2. WAKE / HIBERNATE / MIGRATE LATENCY BENCHMARK"
echo "==================================================================="
# Ensure starting from hibernated + detached state
if [[ $(kubectl -n agent-lab get pods -l app=opencode-sre --no-headers 2>/dev/null | wc -l) -gt 0 ]]; then
  measure_hibernate "Pre-clean Hibernate"
fi

measure_wake "Wake #1 (initial node)"

POD=$(kubectl -n agent-lab get pod -l app=opencode-sre -o jsonpath='{.items[0].metadata.name}')
NODE1=$(kubectl -n agent-lab get pod "$POD" -o jsonpath='{.spec.nodeName}')

echo "--- Storage I/O & fsync benchmark on Longhorn 2-replica volume (/data) ---"
kubectl -n agent-lab exec "$POD" -- sh -c '
  dd if=/dev/zero of=/data/bench.tmp bs=1M count=64 conv=fsync 2>&1 | tail -n 1
  dd if=/dev/zero of=/data/bench_4k.tmp bs=4k count=500 conv=fsync 2>&1 | tail -n 1
  rm -f /data/bench.tmp /data/bench_4k.tmp
'

echo "--- OpenCode CLI execution latency & RSS inside container ---"
T_OC0=$(now_ms)
kubectl -n agent-lab exec "$POD" -- sh -c 'opencode debug paths'
T_OC1=$(now_ms)
echo "opencode debug paths execution time: $((T_OC1 - T_OC0)) ms"

measure_hibernate "Hibernate #1 (from $NODE1)"

echo "--- Migrating from $NODE1 to peer node ---"
kubectl cordon "$NODE1" >/dev/null
cleanup() { kubectl uncordon "$NODE1" >/dev/null 2>&1 || true; }
trap cleanup EXIT

measure_wake "Wake #2 (Migrate to peer node)"
NODE2=$(kubectl -n agent-lab get pod -l app=opencode-sre -o jsonpath='{.items[0].spec.nodeName}')
kubectl uncordon "$NODE1" >/dev/null

# Second hibernate + wake on NODE2 to measure warm wake (image already cached on NODE2)
measure_hibernate "Hibernate #2 (from $NODE2)"
kubectl cordon "$NODE1" >/dev/null
measure_wake "Wake #3 (Warm Wake on $NODE2, image cached)"
kubectl uncordon "$NODE1" >/dev/null
trap - EXIT
