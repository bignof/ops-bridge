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
- 支持 `update` / `restart` / `drain` 平台命令；restart/update 可带 `drain: true`，在同一条命令里先下线排空再重启
- 支持 `plugin_scan` / `plugin_remove` / `plugin_restore` 业务容器插件文件操作
- 巡检上报包含已停止的容器（`docker compose ps --all`），一次性容器不上报
- 连接时上报版本与能力清单（`agent_report.runtime.capabilities`），Hub 据此决定可用功能
- 支持 `logs_start` / `logs_stop` 日志流会话，用于实时查看 `docker compose logs -f --tail N`
- 支持 `logfile_list` / `logfile_fetch` / `logfile_follow` 日志文件协议：列出部署目录下的日志文件、把某个文件 gzip 上传到中枢归档、实时跟随子目录里最新的日志文件
- 支持 `compose_discover` / `compose_inspect`：只读扫描 compose 项目并汇报服务 / 容器名 / 镜像 / 端口
- 与中枢断连时自动停掉全部日志会话与进行中的上传
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
| `WS_URL`              | 控制台 WebSocket 地址         | `ws://192.168.1.10:13000/ws/agent`                   |
| `AGENT_ID`            | Agent 唯一标识                | `prod-server-01`                                     |
| `AGENT_KEY`           | Hub 为该 agent 签发的独立 key | `hub-issued-agent-key`                               |
| `AGENT_AUTH_MODE`     | key 的传递方式：`auto`（默认，先用 `X-Agent-Key` 请求头，旧版 Hub 拒绝时自动改用 URL 参数）、`header`、`query` | `auto` |
| `RECONNECT_DELAY`     | 断线重连间隔（秒），默认 `5`  | `5`                                                  |
| `HEARTBEAT_INTERVAL`  | 心跳间隔（秒），默认 `30`     | `30`                                                 |
| `STATUS_REPORT_INTERVAL` | 定时巡检上报间隔（秒），默认 `120` | `120`                                          |
| `PLUGIN_FOLLOW_UP_INTERVAL` | restart/update 后跟踪插件同步的采集间隔（秒），默认 `10` | `10`                     |
| `PLUGIN_FOLLOW_UP_TIMEOUT` | 跟踪插件同步的最长时长（秒），默认 `300`；同步结束，或容器启动 90 秒仍无本次同步回执即提前停止 | `300` |
| `HEALTH_PORT`         | 容器内健康检查端口            | `18081`                                              |
| `SERVICE_AGENT_IMAGE` | 运行时拉取的镜像地址          | `registry.example.com/orchidea/service-agent:latest` |
| `HUB_HTTP_URL`        | 中枢 HTTP 基址（日志归档上传用），留空按 `WS_URL` 推导 | `https://hub.example.com`                            |
| `PROJECTS_ROOT`       | compose 项目发现的扫描根（容器内路径），默认 `/data`   | `/data`                                              |
| `APP_HOST`            | 回到宿主机的地址，默认 `host.docker.internal`；`drain` 与健康检查经它 + compose 解析出的映射端口访问被管应用 | `host.docker.internal` |

> ⚠️ **`APP_HOST` 与 `extra_hosts` 必须配套**：agent 自己是容器，与被管的业务容器各在独立 bridge 网络命名空间，
> `127.0.0.1` 只是各自的回环、互相连不到，因此只能走「宿主机地址 + 端口映射」访问对方。默认值
> `host.docker.internal` 在 Linux 上不会自动解析，本仓 `docker-compose.yml` 已配
> `extra_hosts: ["host.docker.internal:host-gateway"]` 补上；**自行编写编排时漏掉这行，`drain` 会报
> `Connection refused`，优雅重启链路整条失效**（2026-09-10 rolltest 实测）。
> 不便用 `extra_hosts` 时改配 `APP_HOST`：裸机进程填 `127.0.0.1`，容器内填宿主机内网 IP。

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

连接与鉴权、版本与能力清单、全部帧的字段与语义统一见 [PROTOCOL.md](PROTOCOL.md)，本文不再重复。

## 健康检查

Agent 容器内会启动一个轻量 HTTP 服务：

```http
GET /health
```

返回内容包含：

- `status`: `ok` 或 `degraded`
- `agentId`: 当前 agent 标识
- `connected`: 当前是否仍与 hub（NocoBase plugin-hub）保持连接
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
│   ├── capabilities.py # 上报给 Hub 的协议能力清单
│   ├── handlers.py
│   ├── log_sessions.py # 实时日志流会话
│   └── ws_client.py
├── services/           # Compose 操作封装
├── requirements.txt    # Python 依赖（含间接依赖全部锁定版本）
├── requirements-dev.txt
├── Dockerfile          # 镜像构建文件
├── docker-compose.yml  # 一键部署配置
├── tests/              # 自动化测试
├── PROTOCOL.md         # 与交付中枢的 WebSocket 协议
└── README.md
```

## 开发 / 本地运行（不使用 Docker）

项目支持通过 `.env` 文件配置参数，示例见 `.env.example`。

```bash
pip install -r requirements-dev.txt

$env:WS_URL="ws://YOUR_HUB_HOST:PORT/ws/agent"
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
- key 默认经 `X-Agent-Key` 请求头传递，不出现在 URL 与代理访问日志里；Hub 全部升级后可把 `AGENT_AUTH_MODE` 固定为 `header`
- 建议在内网环境部署，或通过 TLS（`wss://`）加密 WebSocket 连接
- Docker socket 挂载赋予了 Agent 完整的宿主机容器控制权，请确保只有可信的 ServiceHub 实例能接入

## Agent 自升级

支持交付中枢「服务器 → 升级 Agent」。首次须手工安装带自升级协议的 Agent，之后可从 Hub 指定目标镜像（须带标签或 digest，允许换到其他镜像仓库，服务器须能拉取）升级。

Agent 等待当前服务操作完成后，用当前已安装镜像启动独立的临时执行器。执行器先拉取并固定目标 digest，再仅重建 Agent 服务；新版实际镜像与 Hub 确认、健康检查都通过才算成功。失败自动恢复旧镜像，执行器中断时根据持久化账本补偿，业务容器不重启。

部署需要挂载 Docker socket、Compose 项目文件和持久化状态目录。支持多个 Compose 文件，修改最后一个定义本服务 image 的文件。`AGENT_ID` 须显式配置；自定义 hostname 时可通过 `AGENT_CONTAINER_NAME` 指定自身容器。状态目录默认 `/data/.service-agent/upgrades/`（`AGENT_UPGRADE_DIR` 可覆盖）。私有仓库凭据目录通过 bind mount 与 `DOCKER_CONFIG` 配置，辅助执行器只读使用，不上传凭据。

修改 Compose 时只替换本服务的 `image:` 这一行（保留该行的行尾注释），其余内容、注释和引号保持原样（整份重新序列化会丢注释，并按 YAML 1.1 改写 `22:22`、`on` 这类未加引号的值）。`image:` 必须以单行键值直接写在该服务自己的映射里，不支持流样式、合并键或锚点继承；无法定位时能力探测直接给出原因，不允许远程升级，但照常上报当前镜像。能力探测失败后每 60 秒重试。

原始 Compose 留存于身份对应目录的 `compose.before.yml`；旧版有 digest 时回退配置保留该 digest，首次接入无 digest 的镜像使用保留的本地回退标签。升级成功后的 Compose image 固定为目标 digest；后续手工维护镜像应修改这个实际生效的配置文件。

构建参数 `AGENT_VERSION` 注入版本号，CI 使用 `main-<时间戳>`。上行 `agent_report` 传当前版本、Image ID、自升级能力与持久化任务阶段；Hub 下发 `agent_upgrade`，验证新连接后返回 `agent_upgrade_confirm`。等待中 Agent 重启会恢复同一任务；相同 requestId 不重复执行。

升级期间暂停新的服务操作，日志与插件查询短暂不可用。临时执行器完成后退出，下一次升级清理旧执行器。人工操作 Docker 时须避免与升级任务重叠。

回退只要求旧 Agent 进程响应 `/health`（含未连 Hub 时的 503），不要求连上 Hub，Hub 故障期间也能完成回退。执行器已退出但任务停在非终态超过 2 分钟时，心跳和重连会收敛到终态，避免永久拒绝服务操作：运行镜像、Compose 配置（成功时还要 Hub 确认）三者一致才记 success / rolled_back，否则记 failed 并提示人工核对；读不到当前运行镜像时保留任务。从旧版 Agent 升到本版本的那一次仍由旧执行器执行，以上保护从下一次升级起生效。

执行器启动使用持久化 `launch.json` 交接记录。`launch.json` 本身写入失败时直接记 failed。发布交接后，启动方不再写入 `job.json` 的失败终态；即使 Docker 已启动容器但 CLI 超时/断链，也保留执行器的真实进度与服务操作限制。心跳和重连按固定容器名、Agent 身份标签和本次 requestId 核对执行器；已存在则复用或启动同一个容器，不确定时继续等待，避免覆盖账本或重复创建升级任务。