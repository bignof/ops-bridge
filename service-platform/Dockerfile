# service-platform 运行镜像(P1-SPA:多阶段,前端 SPA + Python 后端同一镜像)。
#
# 阶段 1(web):node 构建 SPA。`npm run build` = `tsc -b && vite build`,vite.config.ts
#   的 `build.outDir` 是相对 web/ 的 `../app/static`,故在 WORKDIR=/build/web 下构建,
#   产物会落到 /build/app/static(零内联脚本:已关 modulepreload polyfill,与严格 CSP 相容)。
# 阶段 2(python):装依赖 + 拷后端代码,再把阶段 1 的 SPA 产物 COPY 进 app/static;
#   app/main.py 在 app/static 存在时 mount StaticFiles("/") 托管 SPA(见 main.py 守卫)。

FROM node:20-slim AS web
WORKDIR /build/web

# 先装依赖,利用层缓存(package*.json 不变则跳过重装)。package-lock.json 已入库 → npm ci。
COPY web/package*.json ./
RUN npm ci

# 源码 + 构建。outDir=../app/static → 产物落 /build/app/static。
COPY web/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖,利用 Docker 层缓存(requirements 不变则跳过重装)。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码 + alembic 配置 + 迁移脚本。
# app/main.py 的 lifespan 启动时自跑 `alembic upgrade head`,故无需单独 migrate 步骤,
# 但必须把 alembic.ini 与 migrations/ 一并 COPY 进镜像,否则启动迁移找不到脚本。
COPY alembic.ini ./alembic.ini
COPY app ./app
COPY migrations ./migrations

# 把阶段 1 构建好的 SPA 产物放到 FastAPI StaticFiles 托管目录(app/static)。
# 注:app/static 在源码树被 .gitignore(构建产物不入库),镜像内由本步注入。
COPY --from=web /build/app/static ./app/static

# 插件包落盘目录(PLUGIN_STORAGE_DIR 缺省 ./data/plugins)。
# 生产须把该目录挂载到持久卷或共享存储(多节点拉包需可达),否则容器重建即丢包。
VOLUME ["/app/data/plugins"]

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
