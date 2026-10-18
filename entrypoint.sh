#!/bin/sh
# 启动脚本：先启动洛雪音源桥接(node)，再启动 FastAPI 主应用

set -e

# 确保数据目录结构
mkdir -p /data/sources

# 启动 lx-bridge（洛雪音源桥接）
echo "[entrypoint] 启动洛雪音源桥接 lx-bridge ..."
node /app/node-bridge/lx-bridge.js &
LX_PID=$!

# 等待 lx-bridge 就绪
sleep 2

# 启动 FastAPI 主应用
echo "[entrypoint] 启动主应用 ..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 2691
