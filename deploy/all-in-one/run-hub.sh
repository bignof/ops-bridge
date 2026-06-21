#!/bin/sh
# hub 启动包装:与 platform 同名的 PORT / DATABASE_URL 在此按进程隔离,
# hub 固定监听容器内 127.0.0.1:8081(只给 nginx + platform 内网调用)。
set -eu

export HOST=127.0.0.1
export PORT=8081
export DATABASE_URL="${HUB_DATABASE_URL:-sqlite:////data/hub/service-hub.db}"

mkdir -p /data/hub
cd /opt/hub
exec uvicorn app.main:app --host 127.0.0.1 --port 8081
