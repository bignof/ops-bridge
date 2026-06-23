# service-agent

部署在内网服务器上的轻量 Docker 代理，通过 WebSocket 连接远程控制台，接收指令后在宿主机上执行 Docker Compose 操作。

## 工作流程

```
1. 内网服务器已通过 docker compose 部署好业务容器（compose 文件已在服务器上）
2. 在该服务器上运行 service-agent
3. 远程平台下发指令：
  - update  →  修改 compose 中匹配服务的 image，然后执行 pull + down + up -d
  - restart →  docker compose restart（重启容器，不重建）
  - logs    →  docker compose logs -f --tail N（持续查看服务日志）
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
- 支持 `logs_start` / `logs_stop` 日志流会话，用于实时查看 `docker compose logs -f --tail N`
- 统一使用 `docker compose`（v2 插件）执行 Compose 命令
- 命令在独立线程中执行，不阻塞心跳和其他消息处理
- 相同 `dir` 的命令严格串行，不同目录的命令可并行执行
- 提供独立 HTTP 健康检查端点，暴露当前 WebSocket 连接状态

## 快速开始

### 前置条件

- 目标服务器已安装 Docker，并提供 `docker compose` v2 插件
- 控制台服务已运行并开放 WebSocket 端口

### 1. 配置参数

可通过环境变量或 `.env` 文件设置，下文示例以 `docker-compose.yml` 为例。设置值后重启容器。

| 变量                  | 说明                          | 示例                                                 |
| --------------------- | ----------------------------- | ---------------------------------------------------- |
| `WS_URL`              | 控制台 WebSocket 地址         | `ws://192.168.1.100:8080/ws/agent`                   |
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

| action    | 执行流程                                                                                                                                                                                        |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `update`  | ① 找到与目标镜像同仓库的服务并更新 `image` → ② `docker compose pull` 成功后才执行切换 → ③ `docker compose down` → ④ `docker compose up -d`；若拉取或启动失败，会恢复原 compose 并尝试拉起旧版本 |
| `restart` | `docker compose restart`                                                                                                                                                                        |

并发约束：如果同一个 Agent 在短时间内收到多条命令，Agent 会按 `dir` 做互斥控制。同一目录上的 `update` / `restart` 会排队串行执行，避免 compose 文件和 Docker 操作互相冲突；不同目录仍允许并行。

### 服务端 → Agent（日志流）

```json
{
  "type": "logs_start",
  "sessionId": "log-123",
  "dir": "/data/dev/admin",
  "tail": 200,
  "timestamps": true
}
```

| 字段         | 类型    | 必填 | 说明 |
| ------------ | ------- | ---- | ---- |
| `type`       | string  | ✅   | `logs_start` 或 `logs_stop` |
| `sessionId`  | string  | ✅   | 日志会话唯一 ID，由 Hub 生成 |
| `dir`        | string  | ✅   | compose 文件所在目录的宿主机绝对路径 |
| `tail`       | integer | 否   | 启动时先输出最近多少行，默认 `200` |
| `timestamps` | boolean | 否   | 是否追加 `docker compose logs --timestamps` |

`logs_stop` 示例：

```json
{
  "type": "logs_stop",
  "sessionId": "log-123"
}
```

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

**日志会话开始：**

```json
{
  "type": "logs_started",
  "sessionId": "log-123",
  "tail": 200,
  "timestamps": true
}
```

**日志分块：**

```json
{
  "type": "logs_chunk",
  "sessionId": "log-123",
  "chunk": "web-1  | service started\n"
}
```

**日志结束：**

```json
{
  "type": "logs_finished",
  "sessionId": "log-123",
  "exitCode": 0,
  "stopped": false,
  "chunks": 32
}
```

**日志错误：**

```json
{
  "type": "logs_error",
  "sessionId": "log-123",
  "error": "Directory not found: /data/dev/admin"
}
```

说明：

- 当前日志能力是单向流式输出，不包含交互式 shell
- Agent 会直接执行 `docker compose logs -f --tail N`，当 Hub 断开该流时会终止对应进程

### 服务端 → Agent（滚动重启：实例发现）

用于零中断滚动重启前，先从 Nacos 拉取某服务的健康实例并与本机运行中的容器做匹配。需要配置 `NACOS_*` 环境变量（见下文）。

```json
{
  "type": "list-instances",
  "requestId": "req-123",
  "serviceName": "memory-share",
  "expectedComposeProject": "memory-share-1"
}
```

| 字段          | 类型   | 必填 | 说明                       |
| ------------- | ------ | ---- | -------------------------- |
| `type`        | string | ✅   | 固定为 `"list-instances"`  |
| `requestId`   | string | ✅   | 请求唯一 ID，原样返回      |
| `serviceName` | string | ✅   | 要查询的 Nacos 服务名      |
| `expectedComposeProject` | string | ❌ | 期望的 compose 工程名（由 `Service.dir` 推得）；传则做寻址漂移校验，容器工程名不符的实例标 `matched=false`。不传则不校验（向后兼容） |

Agent 回复 `list-instances-result`：

```json
{
  "type": "list-instances-result",
  "requestId": "req-123",
  "status": "success",
  "instances": [
    { "address": "192.168.0.30:18029", "containerId": "abcdef123456", "healthy": true, "matched": true, "composeProject": "memory-share-1" }
  ]
}
```

| 字段                    | 类型    | 说明                                                                |
| ----------------------- | ------- | ------------------------------------------------------------------- |
| `status`                | string  | `success` 或 `failed`                                               |
| `instances[].address`   | string  | Nacos 上报的 `ip:port`                                              |
| `instances[].containerId` | string\|null | 与该实例匹配上的本机容器短 ID；未匹配为 `null`                |
| `instances[].healthy`   | boolean | Nacos 视角是否健康（已过滤，恒 `true`）                            |
| `instances[].matched`   | boolean | 是否在本机找到对应的运行中容器；传了 `expectedComposeProject` 且容器工程名不符时为 `false`（寻址漂移） |
| `instances[].composeProject` | string\|null | 匹配容器的 `com.docker.compose.project` label；未匹配或容器无该 label 为 `null`。供上层与 `Service.dir` 推得的工程名比对 |

失败时回 `{ "status": "failed", "error": "..." }`（error 已对 `accessToken` 脱敏）。

> **为什么要校验工程名**：优雅操作是实例级（按 `containerId`，经 nacos serviceName 匹配），force compose 是目录级（按 `Service.dir` 作用该目录全部容器）。若某 service 的 nacos 实例容器与 `Service.dir` 推得的 compose 工程不是同一组容器，优雅与 force 会作用到不同容器，危险。`composeProject` / `matched` 让上层（BFF/hub）据此拒绝漂移实例。

### 服务端 → Agent（滚动重启：优雅重启单个容器）

对单个容器执行「先优雅下线 → 重启 → 等待 ready → 静默观察」的零中断重启流程。

```json
{
  "type": "graceful-restart",
  "requestId": "req-123",
  "containerId": "abcdef123456",
  "healthBaseUrl": "http://192.168.0.30:18029",
  "settleSec": 35,
  "shutdownTimeoutSec": 60,
  "readyTimeoutSec": 180
}
```

| 字段                 | 类型    | 必填 | 默认 | 说明                                                                 |
| -------------------- | ------- | ---- | ---- | -------------------------------------------------------------------- |
| `type`               | string  | ✅   | —    | 固定为 `"graceful-restart"`                                          |
| `requestId`          | string  | ✅   | —    | 请求唯一 ID，原样返回                                                |
| `containerId`        | string  | ✅   | —    | 要重启的本机容器 ID                                                  |
| `healthBaseUrl`      | string  | ✅   | —    | 该实例健康/管理接口基址；**必须为内网 IP**（http/https），公网或域名会被拒绝 |
| `settleSec`          | integer | 否   | 35   | ready 后静默观察秒数（零中断承重步骤，期间不再发流量）              |
| `shutdownTimeoutSec` | integer | 否   | 60   | 调用 `/api/k8s/shutdown` 的超时                                      |
| `readyTimeoutSec`    | integer | 否   | 180  | 等待 `/api/health/ready` 返回 200 的超时（至少探一次）             |

Agent 回复 `graceful-restart-result`（**不发 ack，只回最终结果**）：

```json
{ "type": "graceful-restart-result", "requestId": "req-123", "status": "success" }
```

失败时：`{ "type": "graceful-restart-result", "requestId": "req-123", "status": "failed", "error": "..." }`（error 已脱敏）。

### 服务端 → Agent（滚动重部署：优雅重新拉镜像部署单个容器）

对单个容器执行「先优雅下线 → 拉新镜像并重建 → 等待 ready → 静默观察」的零中断重部署流程；`graceful-restart` 的镜像版。跨机滚动协调器逐实例下发本命令、每步 wait-ready，保证任一时刻至多一个实例 down。

```json
{
  "type": "graceful-redeploy",
  "requestId": "req-123",
  "containerId": "abcdef123456",
  "image": "registry.example.com/app:1.7.0",
  "dir": "/data/biz-app",
  "healthBaseUrl": "http://192.168.0.30:18029",
  "settleSec": 35,
  "shutdownTimeoutSec": 60,
  "readyTimeoutSec": 180
}
```

| 字段                 | 类型    | 必填 | 默认 | 说明                                                                 |
| -------------------- | ------- | ---- | ---- | -------------------------------------------------------------------- |
| `type`               | string  | ✅   | —    | 固定为 `"graceful-redeploy"`                                         |
| `requestId`          | string  | ✅   | —    | 请求唯一 ID，原样返回                                                |
| `image`              | string  | ✅   | —    | 要部署的新镜像；**必须在 agent 的镜像 registry 白名单内**，否则拉取前被拒 |
| `dir`                | string  | ✅   | —    | compose 工程目录（在受管根 `MANAGED_PROJECTS_ROOT` 之下、非 agent 自身目录），镜像重写 / pull / down / up 均在此执行 |
| `healthBaseUrl`      | string  | ✅   | —    | 该实例健康/管理接口基址；**必须为内网 IP**（http/https），公网或域名会被拒绝 |
| `containerId`        | string  | 否   | —    | 当前实例容器 ID（供协调器对账/日志；重建走 compose 目录，不直接按容器操作） |
| `settleSec`          | integer | 否   | 35   | ready 后静默观察秒数（零中断承重步骤，期间不再发流量）              |
| `shutdownTimeoutSec` | integer | 否   | 60   | 调用 `/api/k8s/shutdown` 优雅 drain 的超时                           |
| `readyTimeoutSec`    | integer | 否   | 180  | 等待 `/api/health/ready` 返回 200 的超时（至少探一次）             |

Agent 行为（单实例，与 `graceful-restart` 对称）：drain（`POST /api/k8s/shutdown` 阻塞至排空）→ 拉新镜像并重建（`pull → down → up -d`，失败自动回滚到原 compose）→ 等待新容器 ready → 静默 `settleSec`。任一步失败即停在该步、回 `failed`（compose 重建失败会先走回滚）。

Agent 回复 `graceful-redeploy-result`（**不发 ack，只回最终结果**）：

```json
{ "type": "graceful-redeploy-result", "requestId": "req-123", "status": "success" }
```

失败时：`{ "type": "graceful-redeploy-result", "requestId": "req-123", "status": "failed", "error": "..." }`（error 已脱敏；含镜像非白名单、healthBaseUrl 非内网、drain 失败、重建失败、未在 `readyTimeoutSec` 内 ready 等）。

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
- `commandExecution.activeCommands`: 当前正在执行的目录锁任务数
- `commandExecution.queuedCommands`: 正在等待目录锁的命令数
- `commandExecution.projects`: 按目录展开的执行状态，包含 `projectDir`、`activeRequestId`、`activeAction`、`activeSinceTs`、`queuedCount`

## 项目结构

```
service-agent/
├── agent.py            # Agent 主程序
├── config.py           # 环境变量和运行参数
├── core/               # WebSocket、命令处理、健康检查
│   ├── handlers.py
│   ├── log_sessions.py # 实时日志流会话
│   └── ws_client.py
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
> 如果当前环境只有 `docker-compose` v1 standalone，Agent 会直接报错并拒绝执行命令。

## 测试

```bash
pytest --cov=agent --cov=config --cov=core --cov=services --cov-report=term-missing --cov-fail-under=97 -q
```

## 容器部署说明

- `docker-compose.yml` 已改为只拉取镜像，不再本地 `build`
- 启动前需要先把 `.env.example` 复制为 `.env`，并填好 `SERVICE_AGENT_IMAGE`、`WS_URL`、`AGENT_KEY`
- 健康检查会访问容器内的 `http://127.0.0.1:${HEALTH_PORT}/health`
- agent 镜像内已内置 `docker compose` v2 CLI；如果宿主环境或派生镜像替换了该 CLI，需保证 `docker compose version` 可用
- 宿主机需要正确挂载 Docker Socket 和业务 compose 根目录，否则 Agent 虽然能启动，但无法执行 compose 指令

## 安全建议

- 每个 agent 都应使用 hub 单独签发的 `AGENT_KEY`，不要在多个节点间复用
- 建议在内网环境部署，或通过 TLS（`wss://`）加密 WebSocket 连接
- Docker socket 挂载赋予了 Agent 完整的宿主机容器控制权，请确保只有可信的 ServiceHub 实例能接入
