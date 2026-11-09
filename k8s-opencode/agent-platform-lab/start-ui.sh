#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
VENV_DIR="$ROOT/.private/venv"
PID_FILE="$ROOT/.private/app_chapeau.pid"
PORT_FILE="$ROOT/.private/app_chapeau.port"
LOG_FILE="$ROOT/.private/app_chapeau.log"
export PIP_CACHE_DIR="$ROOT/.private/pip-cache"
export KUBECONFIG="$ROOT/.private/kubeconfig"

BACKGROUND_MODE=0
if [[ "${1:-}" == "--background" || "${1:-}" == "-d" ]]; then
  BACKGROUND_MODE=1
  shift
fi

if [[ ! -f "$KUBECONFIG" ]]; then
  echo "ERROR: Missing $KUBECONFIG. Start the lab first with ./up.sh" >&2
  exit 1
fi

# 1. Stop any already-running instance of App Chapeau so calling start-ui.sh or up.sh restarts it cleanly
EXISTING_PIDS=$(pgrep -f "uvicorn app_chapeau.main:app" || true)
if [[ -f "$PID_FILE" ]]; then
  SAVED_PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [[ -n "$SAVED_PID" ]] && kill -0 "$SAVED_PID" 2>/dev/null; then
    EXISTING_PIDS="$EXISTING_PIDS $SAVED_PID"
  fi
fi

if [[ -n "${EXISTING_PIDS// /}" ]]; then
  echo "==> Stopping existing App Chapeau instance(s) (PIDs: $(echo $EXISTING_PIDS | tr '\n' ' '))..."
  for pid in $EXISTING_PIDS; do
    kill "$pid" 2>/dev/null || true
  done
  # Wait up to 3s for socket release
  for _ in {1..15}; do
    if ! pgrep -f "uvicorn app_chapeau.main:app" >/dev/null 2>&1; then
      break
    fi
    sleep 0.2
  done
  pkill -9 -f "uvicorn app_chapeau.main:app" 2>/dev/null || true
  rm -f "$PID_FILE"
fi

# 2. Auto-resume VMs if agentlab-server is currently shut off
SERVER_STATE=$(virsh -c qemu:///system domstate agentlab-server 2>/dev/null | tr -d '\r\n' || echo "missing")
if [[ "$SERVER_STATE" != "running" ]]; then
  echo "==> agentlab-server is '$SERVER_STATE' -> resuming lab VMs..."
  "$ROOT/resume.sh" "${1:-}"
fi

# 3. Ensure isolated Python venv in .private/venv
if [[ ! -x "$VENV_DIR/bin/uvicorn" ]]; then
  echo "==> Creating isolated Python venv in $VENV_DIR (no host pollution)..."
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --quiet -r "$ROOT/app_chapeau/requirements.txt"
fi

# 4. Find the first free TCP port starting at 8000 (or $PORT if specified)
START_PORT="${PORT:-8000}"
CHOSEN_PORT=$(python3 - "$START_PORT" <<'PY'
import socket, sys
port = int(sys.argv[1])
while port < 65535:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            print(port)
            sys.exit(0)
        except OSError:
            port += 1
sys.exit(1)
PY
)

if [[ "$CHOSEN_PORT" != "$START_PORT" ]]; then
  echo "==> Port $START_PORT is busy -> automatically selected free port $CHOSEN_PORT"
fi
echo "$CHOSEN_PORT" > "$PORT_FILE"
export APP_CHAPEAU_PORT="$CHOSEN_PORT"

if [[ "$BACKGROUND_MODE" -eq 1 ]]; then
  echo "==> Starting App Chapeau in background on http://localhost:$CHOSEN_PORT (logs: $LOG_FILE)..."
  nohup "$VENV_DIR/bin/uvicorn" app_chapeau.main:app \
    --app-dir "$ROOT" \
    --reload \
    --reload-dir "$ROOT/app_chapeau" \
    --host 0.0.0.0 \
    --port "$CHOSEN_PORT" > "$LOG_FILE" 2>&1 &
  APP_PID=$!
  echo "$APP_PID" > "$PID_FILE"
  for _ in {1..25}; do
    if curl -sf "http://127.0.0.1:$CHOSEN_PORT/api/docs" >/dev/null 2>&1; then
      echo "==> App Chapeau is READY on http://localhost:$CHOSEN_PORT (PID: $APP_PID)"
      exit 0
    fi
    sleep 0.2
  done
  echo "==> App Chapeau started (PID: $APP_PID) on http://localhost:$CHOSEN_PORT"
else
  echo "==> Starting App Chapeau Web Console on http://localhost:$CHOSEN_PORT ..."
  exec "$VENV_DIR/bin/uvicorn" app_chapeau.main:app \
    --app-dir "$ROOT" \
    --reload \
    --reload-dir "$ROOT/app_chapeau" \
    --host 0.0.0.0 \
    --port "$CHOSEN_PORT"
fi
