#!/bin/sh
# service-console 启动包装:单进程(hub + platform 已进程内合并为一个 FastAPI app),
# 监听容器内 127.0.0.1:8080,由 nginx 反代对外(80,含 /ws/agent)。
set -eu

export HOST=127.0.0.1
export PORT=8080
export DATABASE_URL="${DATABASE_URL:-sqlite:////data/console/service-console.db}"
export PLUGIN_STORAGE_DIR="${PLUGIN_STORAGE_DIR:-/data/plugins}"

mkdir -p /data/console "$PLUGIN_STORAGE_DIR"
cd /opt/console
exec uvicorn app.main:app --host 127.0.0.1 --port 8080
