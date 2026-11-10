#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -d "frontend/dist" ]; then
  echo "[Flush & Reclaim V5] Building frontend bundle..."
  (cd frontend && npm install --no-audit --no-fund && npm run build)
fi

echo "[Flush & Reclaim V5] Starting local web dashboard at http://127.0.0.1:8000 ..."
exec "$DIR/.venv/bin/uvicorn" backend.app:app --host 127.0.0.1 --port 8000
