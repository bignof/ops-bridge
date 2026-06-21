#!/bin/sh
# platform 启动包装:与 hub 同名的 PORT / DATABASE_URL 在此按进程隔离,
# platform 固定监听容器内 127.0.0.1:8080(由 nginx 反代对外)。
# 缺省把 SERVICE_HUB_URL 指向同容器 hub,HUB_ADMIN_TOKEN 复用 ADMIN_TOKEN——
# 单镜像内一套 token 即可,无需重复配置。
set -eu

export HOST=127.0.0.1
export PORT=8080
export DATABASE_URL="${PLATFORM_DATABASE_URL:-sqlite:////data/platform/service-platform.db}"
export SERVICE_HUB_URL="${SERVICE_HUB_URL:-http://127.0.0.1:8081}"
export HUB_ADMIN_TOKEN="${HUB_ADMIN_TOKEN:-${ADMIN_TOKEN:-}}"
export PLUGIN_STORAGE_DIR="${PLUGIN_STORAGE_DIR:-/data/plugins}"

mkdir -p /data/platform "$PLUGIN_STORAGE_DIR"
cd /opt/platform
exec uvicorn app.main:app --host 127.0.0.1 --port 8080
