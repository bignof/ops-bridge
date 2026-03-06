# service-agent

部署在内网服务器上的轻量 Docker 代理，通过 WebSocket 连接远程控制台，接收指令后在宿主机上执行 Docker Compose 操作。

## 工作流程

```
1. 内网服务器已通过 docker compose 部署好业务容器（compose 文件已在服务器上）
2. 在该服务器上运行 service-agent
3. 远程平台下发指令：
   - update  →  docker-compose pull + down + up -d（更新镜像并重新部署）
   - restart →  docker-compose restart（重启容器，不重建）
   - down    →  docker-compose down（停止并移除容器）
   - up      →  docker-compose up -d（启动）
```

## 架构

```
远程控制台（ServiceHub）
      │  WebSocket (ws://)
      ▼
service-agent（容器）
      │  /var/run/docker.sock + /opt/orchidea/projects（持久化）
      ▼
宿主机 Docker 引擎
```

## 功能

- 通过 WebSocket 与控制台保持长连接，自动断线重连
- compose 文件**持久化存储**在宿主机目录，Agent 重启后项目数据不丢失
- 支持 `update`（pull+down+up）/ `up` / `down` / `restart` / `stop` 等操作
- 自动检测宿主机 `docker compose`（v2 插件）或 `docker-compose`（v1 standalone），优先使用 v2
- 命令在独立线程中执行，不阻塞心跳和其他消息处理

## 快速开始

### 前置条件

- 目标服务器已安装 Docker（包含 `docker compose` 插件 **或** `docker-compose`）
- 控制台服务已运行并开放 WebSocket 端口

### 1. 配置参数

可通过环境变量或 `.env` 文件设置，下文示例以 `docker-compose.yml` 为例。设置值后重启容器。

| 变量                 | 说明                         | 示例                               |
| -------------------- | ---------------------------- | ---------------------------------- |
| `WS_URL`             | 控制台 WebSocket 地址        | `ws://192.168.1.10:13000/ws/agent` |
| `AGENT_ID`           | Agent 唯一标识               | `prod-server-01`                   |
| `TOKEN`              | 认证令牌，需与服务端一致     | `your-secret-token`                |
| `RECONNECT_DELAY`    | 断线重连间隔（秒），默认 `5` | `5`                                |
| `HEARTBEAT_INTERVAL` | 心跳间隔（秒），默认 `30`    | `30`                               |

### 2. 部署

```bash
# 构建并后台启动
docker compose up -d --build

# 查看实时日志
docker compose logs -f
```

### 3. 验证连接

日志中出现以下内容代表成功连接：

```
INFO - Docker client initialized successfully.
INFO - Using 'docker compose' (v2 plugin).
INFO - Connecting to ws://...
INFO - Connected to ServiceHub!
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

## 持久化目录结构

Agent 会在 `PROJECTS_DIR` 下按项目名组织 compose 文件：

```
/opt/orchidea/projects/
├── my-app/
│   └── docker-compose.yml
├── another-project/
│   └── docker-compose.yml
└── ...
```

首次下发 `composeContent` 时自动创建，后续操作直接读取已保存的文件。

## 项目结构

```
service-agent/
├── agent.py            # Agent 主程序
├── requirements.txt    # Python 依赖
├── Dockerfile          # 镜像构建文件
├── docker-compose.yml  # 一键部署配置
└── README.md
```

## 开发 / 本地运行（不使用 Docker）

项目支持通过 `.env` 文件配置参数，示例见 `.env.example`。

```bash
pip install -r requirements.txt

# 如果使用环境变量而非 .env，可以这样设置
export WS_URL=ws://YOUR_SERVICE_HUB_IP:PORT/ws/agent
export AGENT_ID=local-dev
export TOKEN=your-secret-token

python agent.py
```

> **注意**：本地运行时需确保当前环境可访问 Docker socket（`/var/run/docker.sock`）。

## 安全建议

- `TOKEN` 请使用高强度随机字符串，避免使用默认值
- 建议在内网环境部署，或通过 TLS（`wss://`）加密 WebSocket 连接
- Docker socket 挂载赋予了 Agent 完整的宿主机容器控制权，请确保只有可信的 ServiceHub 实例能接入
