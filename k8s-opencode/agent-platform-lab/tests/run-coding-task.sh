#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
export KUBECONFIG="$ROOT/.private/kubeconfig"
MODEL="${OPENCODE_MODEL:-ollama/gpt-oss:20b}"
now_ms() { date +%s%3N; }

# Start user-space TCP relay 192.168.124.1:11434 -> 127.0.0.1:11434 for local Ollama
python3 -c '
import asyncio
async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data); await w.drain()
    except Exception: pass
    finally: w.close()
async def handle(cr, cw):
    sr, sw = await asyncio.open_connection("127.0.0.1", 11434)
    await asyncio.gather(pipe(cr, sw), pipe(sr, cw))
async def main():
    srv = await asyncio.start_server(handle, "192.168.124.1", 11434)
    async with srv: await srv.serve_forever()
asyncio.run(main())
' &
PROXY_PID=$!
trap 'kill $PROXY_PID 2>/dev/null || true' EXIT
sleep 0.3

echo "==> Applying updated ConfigMap and Deployment..."
kubectl apply -f "$ROOT/platform/opencode-configmap.yaml" -f "$ROOT/platform/deployment.yaml"
"$ROOT/tests/hibernate.sh" >/dev/null
"$ROOT/tests/wake.sh" >/dev/null

POD_A=$(kubectl -n agent-lab get pod -l app=opencode-sre -o jsonpath='{.items[0].metadata.name}')
NODE_A=$(kubectl -n agent-lab get pod "$POD_A" -o jsonpath='{.spec.nodeName}')
echo "==> Pod $POD_A running on initial node: $NODE_A"

# Clean previous workspace files for a fresh coding run
kubectl -n agent-lab exec "$POD_A" -- sh -c 'rm -rf /data/workspace/* /data/opencode.db*'

echo ""
echo "==================================================================="
echo " PHASE 1: CODING TASK ON NODE A ($NODE_A) — Model: $MODEL"
echo "==================================================================="
PROMPT_1='Create two files in the current working directory (use relative paths ./rate-limiter.ts and ./rate-limiter.test.ts):
1) rate-limiter.ts: Implement and export a TypeScript class TokenBucketRateLimiter with constructor(options: { capacity: number; refillRatePerSec: number }) and methods:
   - allow(key: string, cost?: number, nowMs?: number): { allowed: boolean; remaining: number; retryAfterMs: number }
   - reset(key: string): void
   - pruneIdle(maxIdleMs: number, nowMs?: number): number (removes buckets unused for > maxIdleMs and returns count removed).
2) rate-limiter.test.ts: Using `import { describe, test, expect } from "bun:test"`, write at least 5 unit tests covering initial burst capacity, rejection with retryAfterMs calculation, token refill over simulated time (nowMs), per-key isolation, and pruneIdle cleanup.
After creating both files, run `bun test` using the bash tool and fix any issues until all tests pass.'

t0=$(now_ms)
kubectl -n agent-lab exec "$POD_A" -- opencode run --standalone --auto --title "RateLimiter-Task" --model "$MODEL" "$PROMPT_1"
t1=$(now_ms)
echo "==> Phase 1 completed in $((t1 - t0)) ms on $NODE_A"

echo "--- Verifying generated files & SQLite DB on $NODE_A ---"
kubectl -n agent-lab exec "$POD_A" -- sh -c '
  ls -lh /data/workspace /data/opencode.db*
  echo "=== Sessions in SQLite ==="
  opencode session list --standalone || true
  echo "=== Running bun test on Node A ==="
  bun test
'

echo ""
echo "==================================================================="
echo " PHASE 2: HIBERNATE & MIGRATE POD + PVC TO PEER NODE"
echo "==================================================================="
t_mig0=$(now_ms)
"$ROOT/tests/migrate.sh"
t_mig1=$(now_ms)
POD_B=$(kubectl -n agent-lab get pod -l app=opencode-sre -o jsonpath='{.items[0].metadata.name}')
NODE_B=$(kubectl -n agent-lab get pod "$POD_B" -o jsonpath='{.spec.nodeName}')
echo "==> Migration $NODE_A -> $NODE_B completed in $((t_mig1 - t_mig0)) ms (New Pod: $POD_B)"

echo ""
echo "==================================================================="
echo " PHASE 3: RESUME SESSION (--continue) ON NODE B ($NODE_B)"
echo "==================================================================="
PROMPT_2='Continue working on ./rate-limiter.ts and ./rate-limiter.test.ts in the current working directory (prefer using the `write` tool rather than multiple small `edit` calls):
Extend TokenBucketRateLimiter constructor options with optional `maxConsecutiveRejections?: number` and `penaltyBoxMs?: number`.
If a key exceeds `maxConsecutiveRejections` rejected calls in a row, block that key in a penalty box for `penaltyBoxMs` milliseconds (immediately returning `{ allowed: false, remaining: 0, retryAfterMs: penaltyBoxMs }` when triggered, and `{ allowed: false, remaining: 0, retryAfterMs: blockedUntil - nowMs }` while blocked) and add `isBlocked(key: string, nowMs?: number): boolean`.
Add unit tests for this Penalty Box behavior in ./rate-limiter.test.ts and run `bun test` until all tests pass.'

t2=$(now_ms)
kubectl -n agent-lab exec "$POD_B" -- opencode run --standalone --auto --continue --model "$MODEL" "$PROMPT_2"
t3=$(now_ms)
echo "==> Phase 3 (--continue on $NODE_B) completed in $((t3 - t2)) ms"

echo "--- Final verification on $NODE_B ---"
kubectl -n agent-lab exec "$POD_B" -- sh -c '
  ls -lh /data/workspace /data/opencode.db*
  echo "=== Final bun test execution on Node B ==="
  bun test
  echo "=== SQLite DB telemetry ==="
  bun -e "
    import { Database } from \"bun:sqlite\";
    const db = new Database(\"/data/opencode.db\", { readonly: true });
    const tables = db.query(\"SELECT name FROM sqlite_master WHERE type=\\\"table\\\"\").all();
    console.log(\"Tables in opencode.db:\", tables.map(t => t.name));
    for (const { name } of tables) {
      const cnt = db.query(\`SELECT count(*) as c FROM \"\${name}\"\`).get();
      console.log(\`  - \${name}: \${cnt.c} rows\`);
    }
  "
'
