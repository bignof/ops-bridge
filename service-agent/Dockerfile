FROM docker:27-cli AS docker-cli

# 使用较新的轻量级 Python 镜像，降低基础镜像漏洞风险
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# Agent 通过挂载的 Docker Socket 调用宿主机 Docker。
# 这里直接从官方 docker CLI 镜像复制 v2 compose 插件，避免落回 v1 standalone。
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY agent.py config.py ./
COPY core/ core/
COPY services/ services/

EXPOSE 18081

# 启动命令
CMD ["python", "agent.py"]