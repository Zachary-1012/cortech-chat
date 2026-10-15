#!/usr/bin/env bash
# Chat Cortech · Linux / macOS 停止脚本
set -uo pipefail

if ! command -v pkill >/dev/null 2>&1; then
  echo "[错误] 未找到 pkill，请手动结束 monitor.app 进程。"
  exit 1
fi

if pkill -f "monitor.app"; then
  echo "已停止 Chat Cortech。"
else
  echo "没有正在运行的 Chat Cortech 服务。"
fi
