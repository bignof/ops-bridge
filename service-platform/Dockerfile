# service-platform 运行镜像(P1a)。
# 单阶段 Python 服务;SPA 前端多阶段构建留待 P1-SPA 计划补充。
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

# 插件包落盘目录(PLUGIN_STORAGE_DIR 缺省 ./data/plugins)。
# 生产须把该目录挂载到持久卷或共享存储(多节点拉包需可达),否则容器重建即丢包。
VOLUME ["/app/data/plugins"]

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
