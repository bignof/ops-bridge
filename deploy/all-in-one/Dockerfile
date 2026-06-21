# syntax=docker/dockerfile:1
# ============================================================================
# service-console 单镜像:service-hub + service-platform + nginx 三合一
#
# 内网运维控制台一体化镜像——一个 `docker run` 起全套,省去 compose 内网编排。
# 多进程由 supervisord 托管;仅 nginx 对外(80),hub/platform 只在容器内 127.0.0.1。
#
# 构建上下文 = monorepo 根(需同时拷 service-hub/ 与 service-platform/):
#   docker build -f deploy/all-in-one/Dockerfile -t service-console:latest .
#
# 进程 / 端口:
#   nginx     :80           ── /ws/agent/ → hub:8081 ; 其余(SPA + /api)→ platform:8080
#   platform  127.0.0.1:8080  BFF + SPA 同源(StaticFiles)
#   hub       127.0.0.1:8081  agent WebSocket 接入 + 内网被 platform 调用
# ============================================================================

# ── 阶段 1:构建前端 SPA ──────────────────────────────────────────────────────
# vite.config.ts 的 build.outDir = 相对 web/ 的 ../app/static,故在 WORKDIR=/build/web
# 下构建,产物落到 /build/app/static(零内联脚本,已关 modulepreload polyfill)。
FROM node:20-slim AS web
WORKDIR /build/web
# 先装依赖,利用层缓存(package*.json 不变则跳过)。lock 已入库 → npm ci。
COPY service-platform/web/package*.json ./
RUN npm ci
COPY service-platform/web/ ./
RUN npm run build

# ── 阶段 2:运行时(python + nginx + supervisor)──────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# nginx(边缘反代)+ supervisor(多进程托管)+ curl(健康检查);装完清缓存、删默认站点。
RUN apt-get update \
  && apt-get install -y --no-install-recommends nginx supervisor curl \
  && rm -rf /var/lib/apt/lists/* \
  && rm -f /etc/nginx/sites-enabled/default

# Python 依赖:platform 的 requirements 是 hub 的严格超集(版本一致),两份都装
# 确保完整,pip 对相同 pin 自去重。
COPY service-platform/requirements.txt /tmp/req-platform.txt
COPY service-hub/requirements.txt /tmp/req-hub.txt
RUN pip install --no-cache-dir -r /tmp/req-platform.txt -r /tmp/req-hub.txt

# hub 代码:放独立目录,避免与 platform 的 app/ migrations/ alembic.ini 同名冲突。
# (各自 cd 到自己目录跑 `uvicorn app.main:app`,alembic 也各读自己的 alembic.ini。)
COPY service-hub/alembic.ini /opt/hub/alembic.ini
COPY service-hub/app /opt/hub/app
COPY service-hub/migrations /opt/hub/migrations

# platform 代码 + 阶段 1 的前端产物(落到 StaticFiles 托管目录 app/static)。
COPY service-platform/alembic.ini /opt/platform/alembic.ini
COPY service-platform/app /opt/platform/app
COPY service-platform/migrations /opt/platform/migrations
COPY --from=web /build/app/static /opt/platform/app/static

# 反代 / 进程托管 / 启动包装。
COPY deploy/all-in-one/nginx.conf /etc/nginx/nginx.conf
COPY deploy/all-in-one/supervisord.conf /etc/supervisor/supervisord.conf
COPY deploy/all-in-one/run-hub.sh /opt/run-hub.sh
COPY deploy/all-in-one/run-platform.sh /opt/run-platform.sh
# 防御 CRLF:Windows 检出可能把脚本写成 CRLF,shebang(#!/bin/sh\r)会失效;统一去 CR。
RUN sed -i 's/\r$//' /opt/run-hub.sh /opt/run-platform.sh /etc/nginx/nginx.conf /etc/supervisor/supervisord.conf \
  && chmod +x /opt/run-hub.sh /opt/run-platform.sh \
  && mkdir -p /data/hub /data/platform /data/plugins

# 持久卷:两个 sqlite 库 + 插件包目录。生产多节点拉包须可达,建议挂共享存储/对象存储。
VOLUME ["/data"]

EXPOSE 80

# 健康检查:经 nginx 打 platform SPA 入口(同时间接验证 nginx + platform 存活)。
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD curl -fsS http://127.0.0.1/ >/dev/null || exit 1

CMD ["supervisord", "-c", "/etc/supervisor/supervisord.conf"]
