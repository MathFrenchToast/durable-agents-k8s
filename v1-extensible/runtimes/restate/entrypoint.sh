#!/bin/sh
set -e

PORT=${PORT:-9080}
RESTATE_ADMIN_URL=${RESTATE_ADMIN_URL:-"http://restate.agent-system.svc.cluster.local:9070"}

# Detect advertise IP or hostname
if [ -n "$ADVERTISE_URI" ]; then
    WORKER_URI="$ADVERTISE_URI"
else
    POD_IP=$(hostname -i 2>/dev/null || echo "127.0.0.1")
    WORKER_URI="http://${POD_IP}:${PORT}"
fi

echo "==> Starting Restate Agent Worker on port ${PORT}..."
uvicorn runtimes.restate.worker:app --host 0.0.0.0 --port "${PORT}" &
UVICORN_PID=$!

echo "==> Waiting for worker to start and registering with Restate at ${RESTATE_ADMIN_URL}..."
# Exponential backoff registration
MAX_RETRIES=30
COUNT=0
REGISTERED=0

while [ $COUNT -lt $MAX_RETRIES ]; do
    sleep 2
    echo "Attempting registration with Restate (attempt $((COUNT+1))/${MAX_RETRIES}) for URI ${WORKER_URI}..."
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${RESTATE_ADMIN_URL}/deployments" \
        -H 'Content-Type: application/json' \
        -d "{\"uri\": \"${WORKER_URI}\", \"use_http_11\": true}" || true)

    if [ "$STATUS" = "200" ] || [ "$STATUS" = "201" ] || [ "$STATUS" = "409" ]; then
        echo "==> Successfully registered worker deployment with Restate! (HTTP $STATUS)"
        REGISTERED=1
        break
    fi
    COUNT=$((COUNT+1))
done

if [ $REGISTERED -ne 1 ]; then
    echo "Warning: Could not register with Restate within timeout. Restate server might register worker later."
fi

# Forward signals and wait for uvicorn process
trap 'kill -TERM $UVICORN_PID' TERM INT
wait $UVICORN_PID
