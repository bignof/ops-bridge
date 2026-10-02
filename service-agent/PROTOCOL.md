# service-agent ↔ 交付中枢 WebSocket 协议

本文是 Agent 与交付中枢（NocoBase `@orchisky/plugin-hub`）之间消息协议的唯一说明。两端任何一侧新增、改名或调整帧字段，都要同步修改本文；Hub 侧实现集中在 `plugin-hub/src/server/services/ws-control-plane.ts`，Agent 侧集中在 `core/ws_client.py` 与 `core/handlers.py`。

所有帧都是 UTF-8 JSON 文本，`type` 字段区分类型。↓ 表示 Hub → Agent，↑ 表示 Agent → Hub。未知 `type` 两端都忽略，不断开连接，也不回错误（当前 Agent 记 debug 日志，旧版 Agent 静默丢弃）。

## 1. 连接与鉴权

- 地址：`<WS_URL>/<AGENT_ID>`，例如 `wss://hub.example.com/ws/agent/prod-server-01`。
- 凭据：Hub 为每台服务器单独签发 `AGENT_KEY`，库里只存指纹。
- 传递方式：
  - 请求头 `X-Agent-Key: <key>`（默认）。key 不进 URL，不会出现在 nginx / 网关访问日志里。
  - URL 参数 `?key=<key>`，只为兼容旧版 Hub 与旧版 Agent。
- Hub 判定：请求带了 `X-Agent-Key` 就只认请求头，不再看 URL 参数；没带请求头才读 `?key=`。鉴权失败以 close code `1008` 断开，reason 区分 key 从哪来：
  - `auth failed: header`：请求头里的 key 被拒（AGENT_ID 不存在或 key 不匹配）。
  - `auth failed`：URL 参数里的 key 被拒，或没带 key。只认 `?key=` 的旧版 Hub 对带请求头的连接也回这个（它没读请求头）。
- 鉴权通过后 Hub 先发 `{"type": "hub_hello", "features": ["header_auth"]}`，声明认识请求头。旧版 Hub 不发。
- Agent 的 `AGENT_AUTH_MODE`：
  - `auto`（默认）：先用请求头。
    - 带请求头被 `auth failed` 拒绝：Hub 没读请求头（旧版 Hub，或新版 Hub 被回滚），改用 URL 参数重连。不管此前请求头是否被接受过。
    - 带请求头被 `auth failed: header` 拒绝：AGENT_ID 或 AGENT_KEY 不对，不回退（回退只会把 key 写进访问日志），只记错误日志。
    - 用 URL 参数连接时收到声明了 `header_auth` 的 `hub_hello`（Hub 已升级），下次重连切回请求头；URL 参数被拒同样切回请求头。
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
  "upgrade": {
    "requestId": "…",
    "targetImage": "oci.example.com/service-agent:main-20261002080000",
    "targetImageId": "sha256:…",
    "previousImageId": "sha256:…",
    "status": "verifying",
    "seq": 3,
    "error": ""
  }
}
```

`upgrade` 是本机自升级账本的快照，没有进行中的任务时为空对象，字段有就带：

| 字段 | 说明 |
| --- | --- |
| `requestId` | 自升级任务 ID，与 `agent_upgrade.requestId` 一致 |
| `targetImage` | 目标镜像全名。Hub 用它核对上报确属当前任务，不一致的上报忽略 |
| `targetImageId` | 拉取后目标镜像的本地 Image ID（`pulling` 之后才有）。Hub 签发 `agent_upgrade_confirm` 时原样带回，Agent 只认与之相同的确认 |
| `previousImageId` | 升级前运行的镜像 ID，用于判断是否已回退 |
| `status` | `waiting` / `pulling` / `verifying` / `success` / `failed` / `rolled_back` 等 |
| `seq` | 单调递增，Hub 据此丢弃乱序的旧快照 |
| `error` | 失败或回退原因 |

`capabilities` 是 Agent 支持的协议能力（定义在 `core/capabilities.py`，Hub 侧常量在 `services/agent-capabilities.ts`）：

| 能力 | 含义 | Hub 的用法 |
| --- | --- | --- |
| `drain_restart` | `command` 的 restart/update 支持 `drain: true`，在同一条命令、同一把目录锁里先下线排空再执行 | 滚动重启/更新时 graceful 实例改发一条合并命令；缺这个能力仍走 drain → 目标 action 两条命令。带 `drain: true` 的帧只发给明确上报了该能力的连接 |
| `status_all` | 巡检包含已停止的容器（`docker compose ps --all`），并剔除 `compose run` 产生的一次性容器 | 离线巡检据此区分说明：没有该能力的 Agent 只报运行中的容器，实例从上报里消失多半是容器已退出 |
| `header_auth` | 支持请求头鉴权 | 仅展示用 |
| `logfile` | 支持 `logfile_*` 日志文件协议 | 缺失时 `logfile_*` 直接返回「不支持」 |
| `compose_inspect` | 支持 `compose_discover` / `compose_inspect` | 同上 |
| `plugin_ops` | 支持 `plugin_scan` / `plugin_remove` / `plugin_restore` 命令 | 同上 |

规则：

- 能力只记在当前连接上；重连后的新连接在收到 `agent_report` 之前能力未知。
- 能力未知（旧 Agent 不上报 `capabilities`，或新连接尚未上报）时 Hub 照常下发：`command` 里不认识的 action 由 Agent 回 `Unsupported action`；`logfile_*`、`compose_*` 这类旧 Agent 不认识的帧会被静默丢弃，Hub 由超时兜底。例外是 `drain: true`：旧 Agent 会忽略这个字段直接重启，所以只发给明确上报了 `drain_restart` 的连接，能力未知时按不支持拒绝。
- 能力已知且缺失时，Hub 直接拒绝，不把帧发过去等超时：命令落库为失败「Agent 版本过旧，不支持该操作，请先升级 Agent」，日志接口返回 HTTP 409。
- 新增能力只追加名字，不改已有名字的含义；名字须匹配 `^[a-z0-9_.-]{1,64}$`。
- `runtime` 同时写入 `o_hub_agent.runtime` 供界面展示。`upgrade` 字段见上表，自升级流程与 `agent_upgrade*` 帧见 README「Agent 自升级」。

## 3. 帧总览

| 帧 | 方向 | 需要的能力 | 用途 |
| --- | --- | --- | --- |
| `hub_hello` | ↓ | — | 鉴权通过后 Hub 发的第一帧，`features` 声明 Hub 支持的连接特性，见第 1 节 |
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

**下线结果**：合并命令先调用应用的下线接口，结果分三种：

- 确认已下线（200）：继续执行目标 action。
- 肯定没生效（连不上、4xx 拒绝）：不动容器，`output` 写 `[info] 下线失败，未执行 <action>，实例保持原状`。
- 没拿到明确结果（读超时、回包中断、5xx、应用回 `success: false`）：下线不可撤销，实例可能已经不接流量，所以再探一次 `/api/health/ready`——已不就绪（503）按已下线继续执行；仍就绪（200）按没生效处理；探不到则不动容器，`output` 写一行含「下线结果未知」的 `[warn]`，提醒人工核对。

**已下线未重启**：合并命令里下线已成功、但重启/启动没有完成时，`result.output` 会带一行
`[warn] 实例已下线（drain 成功）但重启未完成，当前不接流量，请尽快手动重启该实例`。
Hub 以这两行文字判断实例是否可能停在不接流量的状态，改动文案须两端同步（Agent `DRAINED_NOT_RESTARTED` / `DRAIN_UNKNOWN_MARKER`，Hub `AGENT_DRAINED_MARKER` / `AGENT_DRAIN_UNKNOWN_MARKER`）。

命令处理过程中出现任何未预期的异常，Agent 也会回一条 `failed` 的 `result`（handler 已经回过的不再重复回），不会只回 `ack` 让 Hub 干等超时。

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

- 新版 Agent 使用 `docker compose ps --all`，已停止（exited / dead / created）的容器也会上报，运行中的排在前面；`compose run` 的一次性容器不上报（按 `com.docker.compose.oneoff` 标签判断，compose 2.21 之前的输出没有标签字段时从 `docker inspect` 读取）。旧版只报运行中的容器。
- 从未启动过的容器（created）没有启动时间，`startedAt` 为 `null`（不上报 Docker 的零值 `0001-01-01T00:00:00Z`）。
- 一个部署有多个服务时，Hub 按部署记录的镜像仓库认定主服务，其次按实例名、上次记录的容器名（与容器名相同才算）；都认不出时优先取运行中的，最后才取第一个。实例名排在前面，是为了在认错后（如锁在已退出的初始化容器上）能把实例名改成主容器名来纠正。
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
