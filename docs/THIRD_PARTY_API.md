# service-hub 第三方对接 API 文档

本文档面向需要集成 `service-hub` 的平台、运维控制台、调度系统和自动化脚本。

## 适用范围

- 第三方系统应只使用 HTTP API
- Agent 与 Hub 的 WebSocket 协议不属于第三方开放接口范围
- 本文档覆盖查询、下发命令、重试失败命令和审计查询

## 基础信息

- Base URL：`http://<service-hub-host>:8080`
- Content-Type：`application/json`
- 在线文档：`GET /docs`
- OpenAPI：`GET /openapi.json`

## 认证与请求头

当前 HTTP API 没有单独的鉴权中间件，但建议第三方调用时始终补齐以下审计头：

- `X-Requested-By`：调用方身份，例如 `ops-console`、`platform-api`
- `X-Requested-Source`：调用来源，例如 `manual-operation`、`scheduler-job`

这两个头会被持久化到命令记录中，用于后续审计。

## 返回约定

### 成功状态码

- `200 OK`：普通查询成功
- `202 Accepted`：命令已接收并进入队列

### 常见错误状态码

- `404 Not Found`：Agent 或命令不存在
- `409 Conflict`：Agent 离线，或当前命令不允许重试
- `422 Unprocessable Entity`：请求参数或请求体不合法
- `502 Bad Gateway`：Hub 已接收请求，但向 Agent 下发失败

### 通用错误体

```json
{
  "detail": "Agent not found"
}
```

## 数据模型

### AgentSnapshot

```json
{
  "agentId": "prod-server-01",
  "connected": true,
  "online": true,
  "remote": "10.0.0.8:51234",
  "connectedAt": "2026-03-06T10:00:00Z",
  "disconnectedAt": null,
  "lastSeenAt": "2026-03-06T10:01:00Z",
  "lastHeartbeatAt": "2026-03-06T10:01:00Z",
  "lastPongAt": null,
  "staleAfterSeconds": 90
}
```

### CommandSnapshot

```json
{
  "requestId": "c7d99f80-b88e-45fc-a6df-7fe1d9eab1f5",
  "agentId": "prod-server-01",
  "status": "success",
  "action": "restart",
  "dir": "/data/dev/admin",
  "image": null,
  "originalRequestId": null,
  "retryCount": 0,
  "requestedBy": "platform-api",
  "requestSource": "ops-console",
  "payload": {
    "type": "command",
    "requestId": "c7d99f80-b88e-45fc-a6df-7fe1d9eab1f5",
    "action": "restart",
    "dir": "/data/dev/admin"
  },
  "output": null,
  "message": null,
  "error": null,
  "createdAt": "2026-03-06T10:02:00Z",
  "updatedAt": "2026-03-06T10:02:03Z",
  "ackAt": "2026-03-06T10:02:01Z",
  "resultAt": "2026-03-06T10:02:03Z"
}
```

### CommandListResponse

```json
{
  "items": [],
  "total": 0,
  "limit": 50,
  "offset": 0,
  "hasMore": false,
  "sortBy": "updatedAt",
  "order": "desc"
}
```

## API 清单

### 1. 健康检查

```http
GET /health
```

返回：

```json
{
  "status": "ok"
}
```

### 2. 查询全部 Agent

```http
GET /api/agents
```

返回：`AgentSnapshot[]`

### 3. 查询单个 Agent

```http
GET /api/agents/{agentId}
```

### 4. 查询某个 Agent 的命令历史

```http
GET /api/agents/{agentId}/commands?status=failed&requestedBy=platform-api&requestSource=ops-console&createdAfter=2026-03-01T00:00:00Z&createdBefore=2026-03-06T23:59:59Z&sortBy=updatedAt&order=desc&limit=20&offset=0
```

查询参数：

- `status`：可选，命令状态
- `action`：可选，`update` 或 `restart`
- `requestedBy`：可选，审计调用方
- `requestSource`：可选，审计来源
- `createdAfter`：可选，ISO 8601 时间
- `createdBefore`：可选，ISO 8601 时间
- `sortBy`：可选，`createdAt` 或 `updatedAt`
- `order`：可选，`asc` 或 `desc`
- `limit`：可选，默认 `100`，最大 `500`
- `offset`：可选，默认 `0`

返回：`CommandListResponse`

### 5. 查询全局命令列表

```http
GET /api/commands?agentId=prod-server-01&status=success&action=restart&requestedBy=platform-api&requestSource=ops-console&createdAfter=2026-03-01T00:00:00Z&createdBefore=2026-03-06T23:59:59Z&sortBy=updatedAt&order=desc&limit=50&offset=0
```

返回：`CommandListResponse`

### 6. 查询单条命令

```http
GET /api/commands/{requestId}
```

返回：`CommandSnapshot`

### 7. 查询命令审计事件

```http
GET /api/commands/{requestId}/events
```

返回：事件数组，按时间升序。

事件类型目前包括：

- `created`
- `ack`
- `result`
- `retry`

### 8. 下发命令

```http
POST /api/agents/{agentId}/commands
X-Requested-By: platform-api
X-Requested-Source: ops-console
Content-Type: application/json

{
  "requestId": "manual-20260306-0001",
  "action": "restart",
  "dir": "/data/dev/admin"
}
```

`update` 示例：

```json
{
  "requestId": "manual-20260306-0002",
  "action": "update",
  "dir": "/data/dev/admin",
  "image": "nginx:1.27-alpine"
}
```

返回：

```json
{
  "accepted": true,
  "command": {
    "requestId": "manual-20260306-0001",
    "agentId": "prod-server-01",
    "status": "queued",
    "action": "restart",
    "dir": "/data/dev/admin",
    "image": null,
    "originalRequestId": null,
    "retryCount": 0,
    "requestedBy": "platform-api",
    "requestSource": "ops-console",
    "payload": {
      "type": "command",
      "requestId": "manual-20260306-0001",
      "action": "restart",
      "dir": "/data/dev/admin"
    },
    "output": null,
    "message": null,
    "error": null,
    "createdAt": "2026-03-06T10:02:00Z",
    "updatedAt": "2026-03-06T10:02:00Z",
    "ackAt": null,
    "resultAt": null
  }
}
```

### 9. 重试失败命令

```http
POST /api/commands/{requestId}/retry
X-Requested-By: platform-api
X-Requested-Source: ops-console
```

约束：

- 只有 `failed` 状态命令允许重试
- 重试后会生成新的 `requestId`
- 新命令的 `originalRequestId` 指向原失败命令
- 新命令的 `retryCount` 为原命令 `retryCount + 1`

## 推荐对接流程

### 查询 Agent 并下发命令

1. 调用 `GET /api/agents/{agentId}` 确认 `online=true`
2. 调用 `POST /api/agents/{agentId}/commands` 下发命令
3. 记录返回的 `requestId`
4. 轮询 `GET /api/commands/{requestId}` 或读取 `GET /api/commands/{requestId}/events`
5. 如结果为 `failed`，可调用 `POST /api/commands/{requestId}/retry`

### 轮询建议

- 初始轮询间隔建议 `2s`
- 命令执行超过 `queued/processing` 阶段后即可停止轮询
- 需要完整审计时，优先读取事件接口而不是只读单命令状态

## 非兼容变更约定

- 新增字段默认视为向后兼容
- 删除字段、修改字段类型、修改状态语义属于非兼容变更
- 如果后续需要对第三方开放鉴权，会优先在本文档新增说明
