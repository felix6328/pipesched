#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
if [[ -x .venv/bin/uvicorn ]]; then
  UV=.venv/bin/uvicorn
else
  UV=uvicorn
fi
PORT="${PIPESCHED_PORT:-8879}"
echo "PipeSched 工作台 http://127.0.0.1:${PORT}"
exec "$UV" app.main:app --host 127.0.0.1 --port "$PORT"
