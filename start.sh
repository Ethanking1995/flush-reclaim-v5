#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

# 1. Bootstrap Python virtual environment & dependencies if needed
if [ ! -x ".venv/bin/python" ]; then
  echo "[Flush & Reclaim] Creating Python virtual environment (.venv)..."
  python3 -m venv .venv
fi

if ! .venv/bin/python -c "import fastapi, uvicorn, yfinance, pandas_market_calendars" >/dev/null 2>&1; then
  echo "[Flush & Reclaim] Installing Python dependencies..."
  .venv/bin/pip install -q -r requirements.txt
fi

# 2. Build frontend production bundle if missing (or when --build is passed)
if [ "${1:-}" = "--build" ] || [ ! -f "frontend/dist/index.html" ]; then
  echo "[Flush & Reclaim] Building frontend bundle..."
  (cd frontend && npm install --no-audit --no-fund && npm run build)
fi

# 3. Stop any previously running instance of this application
EXISTING_PID="$(pgrep -f 'uvicorn backend.app:app' || true)"
if [ -n "$EXISTING_PID" ]; then
  echo "[Flush & Reclaim] Stopping previous server (PID: ${EXISTING_PID//$'\n'/ })..."
  kill $EXISTING_PID 2>/dev/null || true
  sleep 1
fi

# 4. Fall back to the next open local port if the requested port is busy
if ! .venv/bin/python -c "import socket; s=socket.socket(); s.bind(('$HOST', $PORT)); s.close()" 2>/dev/null; then
  PORT="$(.venv/bin/python -c "import socket
for p in range($PORT + 1, $PORT + 100):
    try:
        s = socket.socket(); s.bind(('$HOST', p)); s.close(); print(p); break
    except OSError: pass")"
  echo "[Flush & Reclaim] Requested port busy — using http://$HOST:$PORT"
fi

echo "[Flush & Reclaim] Starting web application at http://$HOST:$PORT ..."
exec "$DIR/.venv/bin/uvicorn" backend.app:app --host "$HOST" --port "$PORT"
