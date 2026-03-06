# service-agent

部署在内网服务器上的轻量 Docker 代理，通过 WebSocket 连接远程控制台，接收指令后在宿主机上执行 Docker Compose 操作。

## 工作流程

```
1. 内网服务器已通过 docker compose 部署好业务容器（compose 文件已在服务器上）
2. 在该服务器上运行 service-agent
3. 远程平台下发指令：
  - update  →  修改 compose 中匹配服务的 image，然后执行 pull + down + up -d
  - restart →  docker compose restart（重启容器，不重建）
```

## 架构

```
远程控制台（ServiceHub）
      │  WebSocket (ws://)
      ▼
service-agent（容器）
      │  /var/run/docker.sock + /opt/projects（持久化）
      ▼
宿主机 Docker 引擎
```

## 功能

- 通过 WebSocket 与控制台保持长连接，自动断线重连
- 支持 `update` 和 `restart` 两类平台命令
- 自动检测宿主机 `docker compose`（v2 插件）或 `docker-compose`（v1 standalone），优先使用 v2
- 命令在独立线程中执行，不阻塞心跳和其他消息处理
- 提供独立 HTTP 健康检查端点，暴露当前 WebSocket 连接状态

## 快速开始

### 前置条件

- 目标服务器已安装 Docker（包含 `docker compose` 插件 **或** `docker-compose`）
- 控制台服务已运行并开放 WebSocket 端口

### 1. 配置参数

可通过环境变量或 `.env` 文件设置，下文示例以 `docker-compose.yml` 为例。设置值后重启容器。

| 变量                  | 说明                          | 示例                                                 |
| --------------------- | ----------------------------- | ---------------------------------------------------- |
| `WS_URL`              | 控制台 WebSocket 地址         | `ws://192.168.1.10:13000/ws/agent`                   |
| `AGENT_ID`            | Agent 唯一标识                | `prod-server-01`                                     |
| `AGENT_KEY`           | Hub 为该 agent 签发的独立 key | `hub-issued-agent-key`                               |
| `RECONNECT_DELAY`     | 断线重连间隔（秒），默认 `5`  | `5`                                                  |
| `HEARTBEAT_INTERVAL`  | 心跳间隔（秒），默认 `30`     | `30`                                                 |
| `HEALTH_PORT`         | 容器内健康检查端口            | `18081`                                              |
| `SERVICE_AGENT_IMAGE` | 运行时拉取的镜像地址          | `registry.example.com/orchidea/service-agent:latest` |

### 2. 部署

```bash
# 拉取镜像并后台启动
docker compose pull
docker compose up -d

# 查看实时日志
docker compose logs -f

# 查看容器健康状态
docker compose ps
```

### 3. 验证连接

日志中出现以下内容代表成功连接：

```
INFO - Using 'docker compose' (v2 plugin).
INFO - Connecting to ws://...
INFO - Connected to ServiceHub!
INFO - Health server listening on http://0.0.0.0:18081/health
```

## WebSocket 消息协议

### 服务端 → Agent（下发命令）

```json
{
  "type": "command",
  "requestId": "req-123",
  "action": "update",
  "dir": "/data/dev/admin",
  "image": "hello-world:latest"
}
```

| 字段        | 类型   | 必填            | 说明                                                                                                         |
| ----------- | ------ | --------------- | ------------------------------------------------------------------------------------------------------------ |
| `type`      | string | ✅              | 固定为 `"command"`                                                                                           |
| `requestId` | string | ✅              | 请求唯一 ID，原样返回                                                                                        |
| `action`    | string | ✅              | `update` 或 `restart`                                                                                        |
| `dir`       | string | ✅              | compose 文件所在目录的宿主机绝对路径                                                                         |
| `image`     | string | `update` 时必填 | 新镜像全名含 tag（如 `registry/repo:new-tag`）。Agent 自动在 compose 文件中找到同仓库的服务并替换 image 字段 |

#### 支持的 action

| action    | 执行流程                                                                                                                    |
| --------- | --------------------------------------------------------------------------------------------------------------------------- |
| `update`  | ① 修改 compose 文件中对应服务的 `image` 字段 → ② `docker compose pull` → ③ `docker compose down` → ④ `docker compose up -d` |
| `restart` | `docker compose restart`                                                                                                    |

### Agent → 服务端（回复）

**ACK（处理中）：**

```json
{ "type": "ack", "requestId": "req-123", "status": "processing" }
```

**结果（成功）：**

```json
{
  "type": "result",
  "requestId": "req-123",
  "status": "success",
  "output": "=== pull ===\n...\n=== down ===\n...\n=== up -d ===\n...",
  "message": "Action 'update' finished for project 'my-app'."
}
```

**结果（失败）：**

```json
{ "type": "result", "requestId": "req-123", "status": "failed", "error": "..." }
```

## 健康检查

Agent 容器内会启动一个轻量 HTTP 服务：

```http
GET /health
```

返回内容包含：

- `status`: `ok` 或 `degraded`
- `agentId`: 当前 agent 标识
- `connected`: 当前是否仍与 service-hub 保持连接
- `lastConnectTs` / `lastDisconnectTs` / `lastHeartbeatTs` / `lastMessageTs`：ISO 8601 中国时间（`+08:00`）
- `lastError`: 最近一次连接错误

## 项目结构

```
service-agent/
├── agent.py            # Agent 主程序
├── config.py           # 环境变量和运行参数
├── core/               # WebSocket、命令处理、健康检查
├── services/           # Compose 操作封装
├── requirements.txt    # Python 依赖
├── requirements-dev.txt
├── Dockerfile          # 镜像构建文件
├── docker-compose.yml  # 一键部署配置
├── tests/              # 自动化测试
└── README.md
```

## 开发 / 本地运行（不使用 Docker）

项目支持通过 `.env` 文件配置参数，示例见 `.env.example`。

```bash
pip install -r requirements-dev.txt

$env:WS_URL="ws://YOUR_SERVICE_HUB_IP:PORT/ws/agent"
$env:AGENT_ID="local-dev"
$env:AGENT_KEY="hub-issued-agent-key"

python agent.py
```

> **注意**：本地运行时需确保当前环境可访问 Docker socket（`/var/run/docker.sock`）。

## 测试

```bash
pytest --cov=agent --cov=config --cov=core --cov=services --cov-report=term-missing --cov-fail-under=97 -q
```

## 容器部署说明

- `docker-compose.yml` 已改为只拉取镜像，不再本地 `build`
- 启动前需要先把 `.env.example` 复制为 `.env`，并填好 `SERVICE_AGENT_IMAGE`、`WS_URL`、`AGENT_KEY`
- 健康检查会访问容器内的 `http://127.0.0.1:${HEALTH_PORT}/health`
- 宿主机需要正确挂载 Docker Socket 和业务 compose 根目录，否则 Agent 虽然能启动，但无法执行 compose 指令

## 安全建议

- 每个 agent 都应使用 hub 单独签发的 `AGENT_KEY`，不要在多个节点间复用
- 建议在内网环境部署，或通过 TLS（`wss://`）加密 WebSocket 连接
- Docker socket 挂载赋予了 Agent 完整的宿主机容器控制权，请确保只有可信的 ServiceHub 实例能接入
