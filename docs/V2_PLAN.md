# service-hub V2 规划

## 目标

V2 的核心目标不是继续堆接口，而是把当前的“内存态控制服务”升级成“可追溯、可审计、可恢复”的控制平面。

重点能力：

- 命令与执行结果持久化
- Agent 在线状态持久化与历史查询
- 完整审计链路：谁下发、何时下发、原始 payload、执行结果、错误详情
- 服务重启后状态可恢复
- 为后续审批、定时任务、批量任务留出模型空间

## 持久化建议

### 结论

- 生产默认推荐 MySQL
- 单机演示、离线环境、快速试用支持 SQLite
- 最佳做法是两种都支持，通过统一的数据访问层切换

### 为什么不是只选 SQLite

SQLite 的优点：

- 零外部依赖，部署最简单
- 适合单机、PoC、测试环境
- 备份和迁移简单，一个文件即可

SQLite 的问题：

- 并发写入能力有限
- 后续一旦引入审批、批量任务、后台 worker，会更容易碰到锁竞争
- 做可审计平台时，历史数据增长会更快，查询和归档能力不如 MySQL 稳定

### 为什么生产推荐 MySQL

- 更适合多用户、多命令并发写入
- 更适合较长时间保存命令日志和审计数据
- 后续增加索引、分页查询、归档、统计更自然
- 你当前 WSL 已有可复用的 MySQL 8 实例，落地成本很低

## 数据库策略

### 推荐技术栈

- SQLAlchemy 2.x
- Alembic
- Pydantic v2 继续负责 API schema
- PyMySQL 作为 MySQL 驱动

### 连接方式

统一使用 `DATABASE_URL`：

- SQLite：`sqlite:////data/service-hub/service-hub.db`
- MySQL：`mysql+pymysql://root:***@host.docker.internal:3306/service_hub`

本地 WSL 场景建议：

- 复用现有 `mysql8` 容器
- 凭据从 `/data/mysql8/docker-compose.yaml` 读取，不把密码硬编码进仓库
- 如果 service-hub 也运行在 Docker 中，建议在 compose 中增加：
  - `extra_hosts: ["host.docker.internal:host-gateway"]`

## 数据模型

### 1. agents

记录 Agent 的注册与最近状态。

建议字段：

- `id`
- `agent_id`，唯一索引
- `hostname`
- `status`，`online` / `offline` / `stale`
- `remote_addr`
- `connected_at`
- `last_seen_at`
- `last_heartbeat_at`
- `last_pong_at`
- `last_disconnect_at`
- `created_at`
- `updated_at`

### 2. commands

记录每次下发命令的主信息。

建议字段：

- `id`
- `request_id`，唯一索引
- `agent_id`
- `action`
- `target_dir`
- `target_image`
- `status`，`queued` / `processing` / `success` / `failed` / `timeout` / `cancelled`
- `requested_by`
- `request_source`
- `message`
- `error`
- `output`
- `created_at`
- `ack_at`
- `finished_at`

### 3. command_events

这是审计关键表，用于记录命令生命周期事件，不要省略。

建议字段：

- `id`
- `request_id`
- `event_type`，例如 `created` / `ack` / `result` / `timeout` / `retry`
- `payload_json`
- `created_at`

### 4. agent_heartbeats

如果需要做在线率或离线分析，可以单独存心跳表；如果只关心最新状态，可先不做。

## API 演进

### 保留现有接口

现有接口保持兼容：

- `GET /api/agents`
- `GET /api/agents/{agentId}`
- `GET /api/agents/{agentId}/commands`
- `GET /api/commands/{requestId}`
- `POST /api/agents/{agentId}/commands`

### V2 新增接口

- `GET /api/commands`
  - 支持按 `agentId`、`status`、`action`、时间范围分页查询
- `GET /api/commands/{requestId}/events`
  - 查看完整审计事件链
- `GET /api/agents/{agentId}/history`
  - 查看最近在线/离线变化

## 服务内结构调整

### 当前问题

现在的 `HubState` 是纯内存态，适合 v1，但不适合作为长期演进基础。

### V2 建议分层

- `app/api/`
  - FastAPI 路由层
- `app/services/`
  - 命令分发、状态同步、审计写入
- `app/repositories/`
  - 数据库访问抽象
- `app/db/`
  - engine、session、model、migration
- `app/ws/`
  - Agent WebSocket 连接管理

### 关键设计

- WebSocket 连接本身仍放内存，因为连接对象不能持久化
- Agent 最新状态写数据库，保证 hub 重启后还能查到最近已知状态
- 命令创建、ACK、结果都立即写数据库
- 审计事件 append-only，尽量不要覆盖更新

## 版本建议

### V2.0

- 引入数据库抽象
- 支持 SQLite 和 MySQL
- 命令与 Agent 状态持久化
- 审计事件表
- Alembic 初始迁移

### V2.1

- 增加 `requested_by`、简单认证头透传
- 增加命令列表分页查询
- 增加失败重试标记

### V2.2

- 增加批量下发
- 增加审批流预留字段
- 增加超时处理与后台清理任务

## 推荐落地顺序

1. 先做 MySQL + SQLite 双支持，但默认配置仍先跑 SQLite，降低接入门槛。
2. 在你的 WSL 环境里增加一份 `service-hub` 连接到现有 MySQL 8 的本地配置，作为开发默认验证路径。
3. 等审计查询接口跑稳后，再考虑前端页面、审批和批量任务。

## 我建议的最终选择

- 代码层：SQLite 和 MySQL 都支持
- 默认开发环境：SQLite
- 默认生产环境：MySQL
- 你当前这套 WSL：优先直接复用现有 MySQL 8 容器做 V2 开发验证

原因很直接：这样不会为了生产能力牺牲本地开发效率，也不会把未来的审计能力押在 SQLite 的上限上。
