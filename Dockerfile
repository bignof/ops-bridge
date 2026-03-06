# 使用轻量级 Python 镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖 (git, curl 等可选，主要是为了 docker compose 命令如果在容器内调用的话)
# 注意：这里我们是通过 subprocess 调用宿主机的 docker 命令，所以容器内不需要装 docker 引擎
# 但为了保险，如果宿主机是 docker compose v1 (python 脚本)，可能需要装 docker-compose
# 现代宿主机通常有 'docker compose' (v2 插件)，无需额外安装。
# 如果担心环境问题，可以在这里安装 docker-compose-plugin 或 standalone binary
RUN apt-get update && apt-get install -y --no-install-recommends \
  curl \
  && rm -rf /var/lib/apt/lists/*

# 安装 Docker Compose Standalone 作为兜底（若宿主机无 docker compose v2 插件时使用）
RUN curl -SL https://github.com/docker/compose/releases/download/v2.24.0/docker-compose-linux-x86_64 -o /usr/local/bin/docker-compose \
  && chmod +x /usr/local/bin/docker-compose

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY agent.py config.py ./
COPY core/ core/
COPY services/ services/

# 启动命令
CMD ["python", "agent.py"]