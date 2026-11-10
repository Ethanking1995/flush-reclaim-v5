#!/usr/bin/env bash
# Usage:
#   ./start.sh              live HUD, refreshes every 30s
#   ./start.sh once         single HUD snapshot
#   ./start.sh backtest     full V1–V5 backtest
#   ./start.sh watch 2500   override clip size ($ per trade)
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-watch}"
CLIP="${2:-}"

if [ ! -x .venv/bin/python ]; then
  echo "First run: creating .venv and installing dependencies..."
  python3 -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit TARGET_CLIP_USD to your trade size."
fi

ARGS=()
case "$MODE" in
  watch)    ARGS+=(--watch) ;;
  once)     ARGS+=(--live) ;;
  backtest) ;;
  *) echo "Unknown mode '$MODE'. Use: watch | once | backtest"; exit 1 ;;
esac
[ -n "$CLIP" ] && ARGS+=(--clip "$CLIP")

exec .venv/bin/python test_regimes.py "${ARGS[@]+"${ARGS[@]}"}"
