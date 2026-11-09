#!/bin/sh
set -e

PORT=${PORT:-9080}
RESTATE_ADMIN_URL=${RESTATE_ADMIN_URL:-"http://restate:9070"}

if [ -n "$ADVERTISE_URI" ]; then
    WORKER_URI="$ADVERTISE_URI"
else
    WORKER_URI="http://$(hostname -i 2>/dev/null || echo '127.0.0.1'):${PORT}"
fi

echo "Starting Agent Worker on port ${PORT}..."
uvicorn agent_service:app --host 0.0.0.0 --port "${PORT}" &
PID=$!

echo "Registering with Restate at ${RESTATE_ADMIN_URL}..."
for i in $(seq 1 30); do
    sleep 2
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${RESTATE_ADMIN_URL}/deployments" \
        -H 'Content-Type: application/json' \
        -d "{\"uri\": \"${WORKER_URI}\", \"use_http_11\": true}" || true)
    if [ "$STATUS" = "200" ] || [ "$STATUS" = "201" ] || [ "$STATUS" = "409" ]; then
        echo "Worker registered successfully with Restate! (HTTP $STATUS)"
        break
    fi
done

trap 'kill -TERM $PID' TERM INT
wait $PID
