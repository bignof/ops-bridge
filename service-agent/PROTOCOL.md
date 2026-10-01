# service-agent ↔ 交付中枢 WebSocket 协议

本文是 Agent 与交付中枢（NocoBase `@orchisky/plugin-hub`）之间消息协议的唯一说明。两端任何一侧新增、改名或调整帧字段，都要同步修改本文；Hub 侧实现集中在 `plugin-hub/src/server/services/ws-control-plane.ts`，Agent 侧集中在 `core/ws_client.py` 与 `core/handlers.py`。

所有帧都是 UTF-8 JSON 文本，`type` 字段区分类型。↓ 表示 Hub → Agent，↑ 表示 Agent → Hub。未知 `type` 两端都忽略并记日志，不断开连接。

## 1. 连接与鉴权

- 地址：`<WS_URL>/<AGENT_ID>`，例如 `wss://hub.example.com/ws/agent/prod-server-01`。
- 凭据：Hub 为每台服务器单独签发 `AGENT_KEY`，库里只存指纹。
- 传递方式：
  - 请求头 `X-Agent-Key: <key>`（默认）。key 不进 URL，不会出现在 nginx / 网关访问日志里。
  - URL 参数 `?key=<key>`，只为兼容旧版 Hub 与旧版 Agent。
- Hub 判定：请求带了 `X-Agent-Key` 就只认请求头，不再看 URL 参数；没带请求头才读 `?key=`。鉴权失败以 close code `1008`、reason `auth failed` 断开。
- Agent 的 `AGENT_AUTH_MODE`：
  - `auto`（默认）：先用请求头。还没收到过任何消息就被 `1008 auth failed` 拒绝时，判定为不认识请求头的旧版 Hub，改用 URL 参数重连；URL 参数也被拒，再切回请求头（Hub 升级后自动恢复）。同一方式连上并收到过消息后，不再因鉴权失败来回切换。
  - `header`：只用请求头。
  - `query`：只用 URL 参数，供无法透传自定义请求头的代理链路使用。
- 网关与反向代理须透传 `X-Agent-Key` 请求头（Spring Cloud Gateway、nginx 默认透传自定义头）。

## 2. 版本与能力清单

Agent 连上后立即上报，并随每次心跳重复上报 `agent_report`：

```json
{
  "type": "agent_report",
  "runtime": {
    "protocol": 1,
    "bootId": "…",
    "version": "main-20261001120000",
    "image": "oci.example.com/service-agent@sha256:…",
    "imageId": "sha256:…",
    "selfUpgrade": true,
    "capabilities": ["drain_restart", "status_all", "header_auth", "logfile", "compose_inspect", "plugin_ops"]
  },
  "upgrade": { "requestId": "…", "status": "verifying", "seq": 3 }
}
```

`capabilities` 是 Agent 支持的协议能力（定义在 `core/capabilities.py`，Hub 侧常量在 `services/agent-capabilities.ts`）：

| 能力 | 含义 | Hub 的用法 |
| --- | --- | --- |
| `drain_restart` | `command` 的 restart/update 支持 `drain: true`，在同一条命令、同一把目录锁里先下线排空再执行 | 滚动重启/更新时 graceful 实例改发一条合并命令；缺这个能力仍走 drain → 目标 action 两条命令 |
| `status_all` | 巡检包含已停止的容器（`docker compose ps --all`），并剔除 `compose run` 产生的一次性容器 | 仅展示用 |
| `header_auth` | 支持请求头鉴权 | 仅展示用 |
| `logfile` | 支持 `logfile_*` 日志文件协议 | 缺失时 `logfile_*` 直接返回「不支持」 |
| `compose_inspect` | 支持 `compose_discover` / `compose_inspect` | 同上 |
| `plugin_ops` | 支持 `plugin_scan` / `plugin_remove` / `plugin_restore` 命令 | 同上 |

规则：

- 能力只记在当前连接上；重连后的新连接在收到 `agent_report` 之前能力未知。
- 能力未知（旧 Agent 不上报 `capabilities`，或新连接尚未上报）时 Hub 照常下发，由 Agent 自己回错误。
- 能力已知且缺失时，Hub 直接拒绝，不把帧发过去等超时：命令落库为失败「Agent 版本过旧，不支持该操作，请先升级 Agent」，日志接口返回 HTTP 409。
- 新增能力只追加名字，不改已有名字的含义；名字须匹配 `^[a-z0-9_.-]{1,64}$`。
- `runtime` 同时写入 `o_hub_agent.runtime` 供界面展示。`upgrade` 字段与 `agent_upgrade*` 帧见 README「Agent 自升级」。

## 3. 帧总览

| 帧 | 方向 | 需要的能力 | 用途 |
| --- | --- | --- | --- |
| `heartbeat` | ↑ | — | 每 `HEARTBEAT_INTERVAL` 秒一次，`{ts}`；Hub 据此刷新在线状态 |
| `ping` / `pong` | ↓ / ↑ | — | 应用层探活 |
| `agent_report` | ↑ | — | 版本、能力与自升级进度，见第 2 节 |
| `agent_upgrade` / `agent_upgrade_confirm` | ↓ | — | Agent 自升级，见 README |
| `command` | ↓ | 视 action 与 `drain` 而定 | 执行 compose 操作，见第 4 节 |
| `ack` / `result` | ↑ | — | 命令受理与结果 |
| `result_ack` | ↓ | — | Hub 已把 `result` 落库，Agent 出站队列清账 |
| `watch_targets` | ↓ | — | 该 Agent 名下需要巡检的部署全集，见第 5 节 |
| `status_report` | ↑ | — | 容器状态与插件盘点，见第 5 节 |
| `plugin_query` / `plugin_query_result` | ↑ / ↓ | — | 业务容器启动时拉取插件发布清单，见第 6 节 |
| `logs_start` / `logs_stop` 与 `logs_*` | ↓ / ↑ | — | `docker compose logs -f` 实时流，见第 7 节 |
| `logfile_*` | ↓ / ↑ | `logfile` | 日志文件列表、归档上传、实时跟随，见第 8 节 |
| `compose_discover` / `compose_inspect` 与结果帧 | ↓ / ↑ | `compose_inspect` | 只读扫描 compose 项目，见第 9 节 |

## 4. 命令

```json
{
  "type": "command",
  "requestId": "req-123",
  "action": "update",
  "dir": "/data/dev/admin",
  "image": "registry/repo:new-tag",
  "graceful": true,
  "drain": true,
  "shutdownToken": "…"
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `requestId` | ✅ | 请求唯一 ID，ack / result 原样返回 |
| `action` | ✅ | 见下表 |
| `dir` | ✅ | compose 文件所在目录的宿主机绝对路径 |
| `image` | update 必填 | 新镜像全名含 tag；Agent 在 compose 里找同仓库的服务替换 `image` |
| `graceful` | 否 | 命令成功后再等待应用自报健康，确认后才回 success |
| `drain` | 否 | restart/update 前先在同一条命令里下线排空，需要 `drain_restart` 能力 |
| `shutdownToken` | drain 时使用 | 调用应用下线接口时携带的令牌 |
| `plugin` | 插件命令必填 | 插件文件操作参数 |

| action | 执行流程 |
| --- | --- |
| `update` | 改 compose 中同仓库服务的 `image` → `pull` 成功后才切换 →（`drain: true` 时此处下线排空，失败则恢复 compose 并结束，实例保持原状）→ `down` → `up -d`；拉取或启动失败会恢复原 compose 并尝试拉起旧版本 |
| `restart` | （`drain: true` 时先下线排空，失败则不重启）→ `docker compose restart` |
| `drain` | 只调用应用下线接口；两步模式（旧 Agent）的第一步 |
| `plugin_scan` / `plugin_remove` / `plugin_restore` | 业务容器插件目录盘点与文件操作，需要 `plugin_ops` 能力；结果额外带 `pluginReport` |

同一 `dir` 的命令按目录锁串行，不同目录可并行。Agent 自升级进行中拒绝新命令。

**已下线未重启**：合并命令里下线已成功、但重启/启动没有完成时，`result.output` 会带一行
`[warn] 实例已下线（drain 成功）但重启未完成，当前不接流量，请尽快手动重启该实例`。
Hub 以这行文字判断实例是否停在不接流量的状态，改动文案须两端同步（Agent `DRAINED_NOT_RESTARTED`，Hub `AGENT_DRAINED_MARKER`）。

回复：

```json
{ "type": "ack", "requestId": "req-123", "status": "processing" }
{ "type": "result", "requestId": "req-123", "status": "success", "output": "=== pull ===\n…", "message": "…" }
{ "type": "result", "requestId": "req-123", "status": "failed", "error": "…", "output": "…" }
```

`result` 进 Agent 本地出站队列，至少送达一次：直到收到 `{"type":"result_ack","requestId":"req-123"}` 才清账，断线或未确认时按退避补投。Hub 对同一 `requestId` 的重复 `result` 幂等处理；命令已被超时兜底置为失败后才到的结果只追加到 output 留痕，不覆盖终态。

## 5. 状态巡检

Hub 在 Agent 连上和部署变更时下发该 Agent 名下的完整清单（覆盖式，不是增量）：

```json
{ "type": "watch_targets", "targets": [{ "deploymentId": 12, "dir": "/data/dev/admin", "service": "done-admin" }] }
```

Agent 每 `STATUS_REPORT_INTERVAL` 秒、命令结束后以及每条新连接收到首份清单时上报：

```json
{
  "type": "status_report",
  "reports": [
    {
      "deploymentId": 12,
      "services": [
        {
          "name": "app",
          "image": "registry/repo:tag",
          "state": "exited",
          "startedAt": "2026-10-01T08:00:00+08:00",
          "containerName": "admin-app-1",
          "containerId": "…",
          "plugins": { "schemaVersion": 1, "status": "ok", "entries": [], "sync": {} }
        }
      ]
    }
  ]
}
```

- 新版 Agent 使用 `docker compose ps --all`，已停止（exited / dead / created）的容器也会上报，`compose run` 的一次性容器不上报。旧版只报运行中的容器。
- 一个部署有多个服务时，Hub 按部署记录的镜像仓库认定主服务，其次按上次记录的容器名、实例名，最后才取第一个。
- `plugins` 是容器内插件盘点与最近一次启动同步回执，字段定义见 plugin-hub `plugin-inventory.ts`。

## 6. 插件清单

业务容器启动时，同步脚本经本机 Agent 向 Hub 查询该服务的插件发布清单：

```json
{ "type": "plugin_query", "requestId": "q-1", "service": "done-admin", "manifestVersion": 2 }
{ "type": "plugin_query_result", "requestId": "q-1", "plugins": [
  { "pluginName": "@business/plugin-x", "version": "1.2.0", "url": "https://…/x-1.2.0.tgz",
    "releaseId": "34", "sha256": "e3b0c442…" }
] }
```

- `manifestVersion: 2` 时 Hub 才返回 `releaseId` 与 `sha256`；旧请求只有前三个字段。Agent 原样转交，不解释插件字段。
- `sha256` 是插件包内容摘要（小写 hex）。同步脚本下载后校验，不一致拒绝安装并保留旧版本；上线摘要之前上传的版本在 Hub 启动时回填，读不到文件的没有该字段，脚本跳过校验。
- 下载插件包时，同步脚本只对 Hub 自身地址（及 `downloadAuthOrigins` 显式列出的地址）附带管理令牌，外部 CDN 地址不带。
- Hub 找不到服务或该 Agent 未部署此服务时返回 `plugins: []` 与 `error: "not_found"`；Hub 内部出错返回 `error: "unavailable"`。不能把带 `error` 的空列表当成「没有插件」。

## 7. 实时日志（logs_*）

```json
{ "type": "logs_start", "sessionId": "log-123", "dir": "/data/dev/admin", "tail": 200, "timestamps": true }
{ "type": "logs_stop", "sessionId": "log-123" }
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `sessionId` | ✅ | 日志会话唯一 ID，由 Hub 生成 |
| `dir` | logs_start 必填 | compose 文件所在目录 |
| `tail` | 否 | 启动时先输出最近多少行，默认 `200` |
| `timestamps` | 否 | 是否追加 `--timestamps` |

Agent 回：`logs_started {sessionId, tail, timestamps}`、`logs_chunk {sessionId, chunk}`、`logs_finished {sessionId, exitCode, stopped, chunks}`、`logs_error {sessionId, error}`。只有单向流式输出，不提供交互式 shell；Hub 结束会话或连接断开时 Agent 终止对应进程。

## 8. 日志文件（logfile_*）

路径安全：`dir` 必须是含 compose 文件的目录；日志根 = `dir/<logDir 或 logs>`，realpath 后必须在 `dir` 之内；`file` / `subdir` 相对日志根，越界一律 `forbidden`；只认 `*.log` 与 `*.log.N`。

| 帧 | 方向 | 字段 |
| --- | --- | --- |
| `logfile_list` | ↓ | `requestId`, `dir`, `logDir?` |
| `logfile_list_result` | ↑ | `requestId`, `root`, `files: [{path,size,mtime}]`, `error?`（`mtime` 为带时区偏移的 ISO 8601，如 `2026-09-05T17:30:15+08:00`；`logfile_fetch` 上传的 `X-Hub-File-Mtime` 同此格式） |
| `logfile_fetch` | ↓ | `requestId`, `archiveId`, `dir`, `logDir?`, `file`, `uploadPath`, `uploadToken`, `uploadExpiresAt` |
| `logfile_fetch_result` | ↑ | `requestId`, `archiveId`, `ok`, `sizeRaw?`, `sizeSent?`, `error?`（旁路通知，状态真源是 HTTP 上传） |
| `logfile_follow` | ↓ | `sessionId`, `dir`, `logDir?`, `subdir`, `tail?`(默认 200，≤500), `filter?` |
| `logfile_started` | ↑ | `sessionId`, `file`, `fileSize` |
| `logfile_entries` | ↑ | `sessionId`, `seq`, `file`, `startOffset`, `endOffset`, `entries: [{offset,text,timestamp,level}]`, `dropped` |
| `logfile_rotated` | ↑ | `sessionId`, `from`, `to` |
| `logfile_finished` | ↑ | `sessionId`, `reason`（`unfollow` / `file_gone` / `error`） |
| `logfile_error` | ↑ | `sessionId`, `error: {code,message}` |
| `logfile_unfollow` | ↓ | `sessionId` |

`filter`：`{ levels?: string[], keyword?: string, regex?: boolean, since?: 'YYYY-MM-DD HH:mm:ss', until?: ... }`；以「条目」（首行 + 堆栈续行）为单位匹配，follow 忽略 since/until。上传：agent 边 gzip 边 `POST <HUB_HTTP_URL 或 WS_URL 推导><uploadPath>`，头 `Content-Type: application/gzip`、`X-Hub-Upload-Token`、`X-Hub-Raw-Size`、`X-Hub-File-Mtime`。限制：跟随会话同时最多 3 条、上传同时 1 个，超出回 `busy`；每秒超过 2000 条的条目直接丢并在 `dropped` 报数。与 hub 断连时全部跟随会话、进行中的上传与旧的 `logs_*` 会话一律停止。

## 9. compose 发现

| 帧 | 方向 | 字段 |
| --- | --- | --- |
| `compose_discover` | ↓ | `requestId` |
| `compose_discover_result` | ↑ | `requestId`, `root`, `projects: [{dir, composeFile, services, error?}]`, `truncated`, `error?` |
| `compose_inspect` | ↓ | `requestId`, `dir` |
| `compose_inspect_result` | ↑ | `requestId`, `dir`, `composeFile`, `services: [{name, containerName, image, ports}]`, `error?` |

发现只扫 `PROJECTS_ROOT` 向下 3 层、跳过 `.` 开头目录与 `node_modules`、找到项目不再下钻、最多 200 个；两者都只读文件、不执行 docker。错误码：`not_found` / `invalid` / `io_error`。
