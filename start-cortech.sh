#!/usr/bin/env bash
# Chat Cortech · Linux / macOS 启动脚本（需 Python 3.10+）
# 用法：./start-cortech.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
HOST="${XHS_HOST:-127.0.0.1}"
PORT="${XHS_PORT:-8000}"

# 优先使用随包分发的离线依赖 libs/（存在则注入 PYTHONPATH）
if [ -d libs ]; then
  export PYTHONPATH="$PWD/libs${PYTHONPATH:+:$PYTHONPATH}"
  export PYTHONNOUSERSITE=1
fi

mkdir -p data

if ! "$PY" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "[错误] 缺少依赖，请先执行："
  echo "       $PY -m pip install -r monitor/requirements.txt"
  exit 1
fi

if curl -fsS -m 2 "http://$HOST:$PORT/api/auth/status" >/dev/null 2>&1; then
  echo "服务已在运行：http://$HOST:$PORT"
  exit 0
fi

echo "Chat Cortech 启动中… 访问 http://$HOST:$PORT"
echo "（按 Ctrl+C 停止）"
exec "$PY" -m monitor.app
