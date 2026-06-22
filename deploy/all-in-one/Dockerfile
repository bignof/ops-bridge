# syntax=docker/dockerfile:1
# ============================================================================
# service-console 单镜像:单一 FastAPI(hub+platform 已进程内合并)+ nginx 二合一
#
# 内网运维控制台一体化镜像——一个 `docker run` 起全套。
# 进程由 supervisord 托管;仅 nginx 对外(80),console 只在容器内 127.0.0.1:8080。
#
# 构建上下文 = monorepo 根:
#   docker build -f deploy/all-in-one/Dockerfile -t service-console:latest .
#
# 进程 / 端口:
#   nginx    :80            ── /ws/agent/ 与 其余(SPA + /api) 全部 → console:8080
#   console  127.0.0.1:8080   单一 FastAPI:BFF + SPA(StaticFiles)+ hub 路由 + /ws/agent
# ============================================================================

# ── 阶段 1:构建前端 SPA ──────────────────────────────────────────────────────
# vite.config.ts 的 build.outDir = 相对 web/ 的 ../app/static,故在 WORKDIR=/build/web
# 下构建,产物落到 /build/app/static(零内联脚本,已关 modulepreload polyfill)。
FROM node:20-slim AS web
WORKDIR /build/web
COPY service-console/web/package*.json ./
RUN npm ci
COPY service-console/web/ ./
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

# Python 依赖(单一 console requirements)。
COPY service-console/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt

# console 代码(含进程内并入的 app/hub)+ 阶段 1 前端产物(落到 StaticFiles 目录 app/static)。
COPY service-console/alembic.ini /opt/console/alembic.ini
COPY service-console/app /opt/console/app
COPY service-console/migrations /opt/console/migrations
COPY --from=web /build/app/static /opt/console/app/static

# 反代 / 进程托管 / 启动包装。
COPY deploy/all-in-one/nginx.conf /etc/nginx/nginx.conf
COPY deploy/all-in-one/supervisord.conf /etc/supervisor/supervisord.conf
COPY deploy/all-in-one/run-console.sh /opt/run-console.sh
# 防御 CRLF:Windows 检出可能把脚本写成 CRLF,shebang(#!/bin/sh\r)会失效;统一去 CR。
RUN sed -i 's/\r$//' /opt/run-console.sh /etc/nginx/nginx.conf /etc/supervisor/supervisord.conf \
  && chmod +x /opt/run-console.sh \
  && mkdir -p /data/console /data/plugins

# 持久卷:单一 sqlite 库 + 插件包目录。生产多节点拉包须可达,建议挂共享存储/对象存储。
VOLUME ["/data"]

EXPOSE 80

# 健康检查:经 nginx 打 console SPA 入口(同时间接验证 nginx + console 存活)。
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD curl -fsS http://127.0.0.1/ >/dev/null || exit 1

CMD ["supervisord", "-c", "/etc/supervisor/supervisord.conf"]
