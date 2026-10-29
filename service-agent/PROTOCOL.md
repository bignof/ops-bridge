# service-agent ↔ 交付中枢 WebSocket 协议

本文是 Agent 与交付中枢（NocoBase `@orchisky/plugin-hub`）之间消息协议的唯一说明。两端任何一侧新增、改名或调整帧字段，都要同步修改本文；Hub 侧实现集中在 `plugin-hub/src/server/services/ws-control-plane.ts`，Agent 侧集中在 `core/ws_client.py` 与 `core/handlers.py`。

业务服务经本机 Agent 转给 Hub 的请求（本机 HTTP 端点，实现在 `core/health_server.py`）也记在本文：拉插件清单（第 6 节）、运维口令校验（第 10 节）、环境开关（第 11 节）、实例快照（第 12 节）。其中第 10～12 节的一问一答共用 `core/hub_channel.py` 的传输层，运维口令校验在 `core/ops_code.py`，环境开关和实例快照在 `core/hub_rpc.py`。

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
- 鉴权通过后 Hub 先发 `{"type": "hub_hello", "features": ["header_auth", "ops_code", "plugin_query_env", "env_config", "instance_snapshot"]}`。`features` 声明 Hub 支持的连接特性；旧版 Hub 不发 `hub_hello`，或 `features` 里没有后加的特性：

  | 特性 | 含义 |
  | --- | --- |
  | `header_auth` | 认识请求头鉴权（本节） |
  | `ops_code` | 支持运维口令校验（第 10 节） |
  | `plugin_query_env` | 认识 `plugin_query.withEnv`：`plugin_query_result` 可带 `env`，也可能回 `not_bound`（第 6 节） |
  | `env_config` | 支持环境开关查询 `env_config_query`（第 11 节） |
  | `instance_snapshot` | 支持实例快照查询 `instance_snapshot_query`（第 12 节） |

  Agent 按特性取舍：缺 `ops_code`、`env_config`、`instance_snapshot` 时，对应的本机端点回 501；`plugin_query_env` 不拦截请求，应用要了 `withEnv` 照样转给 Hub，旧 Hub 不认就忽略，结果里没有 `env`，`/queryPlugin` 仍回纯数组。
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
    "capabilities": ["drain_restart", "status_all", "header_auth", "logfile", "compose_inspect", "plugin_ops", "ops_code", "env_config", "instance_snapshot", "plugin_query_env", "discover_sync_service"]
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
| `ops_code` | 支持本机 `/verifyOpsCode` 与 `ops_code_verify` 帧（第 10 节） | 仅展示、排障和发布前检查用；Hub 是否下发结果只看自己，不看这个能力 |
| `env_config` | 支持本机 `/envConfig` 与 `env_config_query` 帧（第 11 节） | 同 `ops_code`：仅展示、排障和发布前检查用，Hub 回不回结果只看自己 |
| `instance_snapshot` | 支持本机 `/instanceSnapshot` 与 `instance_snapshot_query` 帧（第 12 节） | 同 `ops_code`：仅展示、排障和发布前检查用，Hub 回不回结果只看自己 |
| `plugin_query_env` | 支持 `/queryPlugin?withEnv=1`：请求帧带 `withEnv`，把结果里的 `env` 转给应用，认识 `not_bound`（第 6 节） | Hub 只对上报了该能力的 Agent 回 `not_bound`，其余 Agent 收到 `not_found` |
| `discover_sync_service` | `compose_discover_result.projects[]` 带 `syncService` / `syncConfigError`（第 9 节） | 界面据此比对目录里配的拉取编码；缺该能力的 Agent，结果里没有这两个字段，按「拉取编码未知」显示 |

规则：

- 能力只记在当前连接上；重连后的新连接在收到 `agent_report` 之前能力未知。
- 能力未知（旧 Agent 不上报 `capabilities`，或新连接尚未上报）时 Hub 照常下发：`command` 里不认识的 action 由 Agent 回 `Unsupported action`；`logfile_*`、`compose_*` 这类旧 Agent 不认识的帧会被静默丢弃，Hub 由超时兜底。例外是 `drain: true`：旧 Agent 会忽略这个字段直接重启，所以只发给明确上报了 `drain_restart` 的连接，能力未知时按不支持拒绝。
- 能力已知且缺失时，Hub 直接拒绝，不把帧发过去等超时：命令落库为失败「Agent 版本过旧，不支持该操作，请先升级 Agent」，日志接口返回 HTTP 409。
- 上表后四项（`env_config`、`instance_snapshot`、`plugin_query_env`、`discover_sync_service`）不是 Hub 下发命令的前提：缺失时 Hub 不拒绝任何命令，对应的查询或字段只是没有，Hub 和应用侧按「没有这个信息」降级，旧 Agent 照常工作。
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
| `plugin_query` / `plugin_query_result` | ↑ / ↓ | — | 业务容器启动时拉取插件发布清单，可顺带取环境开关，见第 6 节 |
| `logs_start` / `logs_stop` 与 `logs_*` | ↓ / ↑ | — | `docker compose logs -f` 实时流，见第 7 节 |
| `logfile_*` | ↓ / ↑ | `logfile` | 日志文件列表、归档上传、实时跟随，见第 8 节 |
| `compose_discover` / `compose_inspect` 与结果帧 | ↓ / ↑ | `compose_inspect` | 只读扫描 compose 项目，发现时顺带读目录里配的拉取编码，见第 9 节 |
| `ops_code_verify` / `ops_code_verify_result` | ↑ / ↓ | —（Hub 须在 `hub_hello` 声明 `ops_code`） | 运维口令校验，见第 10 节 |
| `env_config_query` / `env_config_result` | ↑ / ↓ | —（Hub 须在 `hub_hello` 声明 `env_config`） | 应用经本机 Agent 查所在环境的两个配置开关，见第 11 节 |
| `instance_snapshot_query` / `instance_snapshot_result` | ↑ / ↓ | —（Hub 须在 `hub_hello` 声明 `instance_snapshot`） | 应用经本机 Agent 取本应用的实例快照，见第 12 节 |

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

Hub 在 Agent 连上和部署变更时下发该 Agent 名下的完整清单（覆盖式，不是增量；Agent 绑定了多个环境时，是这些环境下的部署全集）：

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
- `plugins.opsGuard` 是业务服务写在插件目录 `.hub-ops-guard.json` 的运维口令保护状态 `{schemaVersion, mode, state, reason, startedAt, updatedAt}`（`@orchisky/plugin-ops-guard` 写入，Agent 只保留这几个字段）。读不到时为 `null`，`plugins.opsGuardError` 说明原因：`missing`（没有该文件，例如镜像不带守卫插件）、`stale`（文件早于本次容器启动，例如镜像回滚后残留）、`invalid`（超过 4KB、符号链接或格式不对）。
- `.hub-ops-guard.json` 的格式要求（写入方必须遵守，任一不满足整份按 `invalid` 处理，Agent 不细分是哪个字段）：UTF-8 JSON 对象，`schemaVersion` 为 `1`；`mode`、`state` 为小写字母开头、只含小写字母、数字和下划线、不超过 64 个字符的代码（`^[a-z][a-z0-9_]{0,63}$`），`reason` 为同样格式的代码或 `null`；`startedAt`、`updatedAt` 为不超过 64 个字符的 ISO 8601 时间字符串（容器启动时间已知时，`startedAt` 解析不了或早于容器启动 2 秒以上记 `stale`）。其他字段忽略。

## 6. 插件清单

业务容器启动时，同步脚本经本机 Agent 向 Hub 查询该服务的插件发布清单。`service` 是应用的拉取编码，Hub 按它找到应用和应用所在的环境：

```json
{ "type": "plugin_query", "requestId": "q-1", "service": "k3f9qm", "manifestVersion": 2, "withEnv": true }
{ "type": "plugin_query_result", "requestId": "q-1", "plugins": [
  { "pluginName": "@business/plugin-x", "version": "1.2.0", "url": "https://…/x-1.2.0.tgz",
    "releaseId": "34", "sha256": "e3b0c442…" }
], "env": { "allowUi": true, "allowDs": false } }
```

- `manifestVersion: 2` 时 Hub 才返回 `releaseId` 与 `sha256`；旧请求只有前三个字段。Agent 原样转交，不解释插件字段。
- `withEnv: true`（`plugin_query`，可选）要求 Hub 在结果里顺带回环境的两个配置开关。Agent 只在应用以 `withEnv=1`（或 `true`）调本机 `/queryPlugin` 时才带这个字段，不要的话帧与以前完全一样。
- `env`（`plugin_query_result`，可选）是 `{"allowUi": boolean, "allowDs": boolean}`，只在请求带 `withEnv: true` 且 Hub 找到应用时给。Agent 只认两个字段都是布尔值的 `env`，形状不合规按 Hub 没回处理（旧 Hub 不认 `withEnv`，也不会回）。带 `error` 时不看 `env`。
- `sha256` 是插件包内容摘要（小写 hex）。同步脚本下载后校验，不一致拒绝安装并保留旧版本；上线摘要之前上传的版本在 Hub 启动时回填，读不到文件的没有该字段，脚本跳过校验。
- 下载插件包时，同步脚本只对 Hub 自身地址（及 `downloadAuthOrigins` 显式列出的地址）附带管理令牌，外部 CDN 地址不带。
- 带 `error` 的结果都是 `plugins: []`，不能把它当成「没有插件」：
  - `not_found`：Hub 找不到这个拉取编码的应用，或这台服务器上没有该应用的实例。
  - `not_bound`：应用存在，但它所在的环境没有绑定这台 Agent 所在的服务器。Hub 只发给上报了 `plugin_query_env` 能力的 Agent，其余 Agent 收到 `not_found`（旧版 Agent 只认 `not_found` / `unavailable`，会把不认识的 `error` 当成没有错误）；不看请求带没带 `withEnv`。
  - `unavailable`：Hub 内部出错。

**本机端点** `GET http://<Agent>:18081/queryPlugin?service=<拉取编码>[&withEnv=1]`

- 鉴权：请求头 `X-Agent-Secret`，`AGENT_LOCAL_SECRET` 未配置 403，不符 401（见第 10 节）。`service` 必填，缺失 400。
- `withEnv` 只认小写的 `1` 或 `true`，其余值（`0`、`false`、空、`yes`、`TRUE`）都当没要。
- 总时限 8 秒（须小于同步脚本硬编码的 15 秒客户端超时）。
- 响应：

  | 情况 | 状态码 | body |
  | --- | --- | --- |
  | 成功，应用没要 `withEnv`（老应用） | 200 | 纯数组 `[PluginItem, …]`，哪怕 Hub 回了 `env` |
  | 成功，应用要了 `withEnv`，Hub 回了合规的 `env` | 200 | `{"plugins": [PluginItem, …], "env": {"allowUi": true, "allowDs": false}}` |
  | 成功，应用要了 `withEnv`，Hub 没回 `env`（旧 Hub，或 `env` 形状不合规） | 200 | 仍是纯数组，应用据此把开关当作未同步 |
  | Hub 回 `not_bound` | 403 | `{"reason": "not_bound"}`，应用据此把它和 Agent 自己拒绝密钥的 403（没有响应体）区分开 |
  | Hub 回 `not_found` | 404 | 无 |
  | Hub 回 `unavailable` | 502 | 无 |
  | 没连上 Hub | 503 | 无 |
  | 8 秒内没收到结果，或请求帧发送失败 | 504 | 无 |

  老应用（不带 `withEnv`）经新版 Agent 查询没绑定的服务，以前得 404，现在得 403 加 reason；两者都不是 200，同步脚本一样记失败并保留本地版本。

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
| `compose_discover_result` | ↑ | `requestId`, `root`, `projects: [{dir, composeFile, services, syncService, syncConfigError?, error?}]`, `truncated`, `error?` |
| `compose_inspect` | ↓ | `requestId`, `dir` |
| `compose_inspect_result` | ↑ | `requestId`, `dir`, `composeFile`, `services: [{name, containerName, image, ports}]`, `error?` |

发现只扫 `PROJECTS_ROOT` 向下 3 层、跳过 `.` 开头目录与 `node_modules`、找到项目不再下钻、最多 200 个；两者都只读文件、不执行 docker。错误码：`not_found` / `invalid` / `io_error`。

**拉取编码**：发现时顺带读各项目目录里的 `sync-plugins.config.json`，带回里面配的拉取编码，界面据此和应用的拉取编码比对。只有 `compose_discover_result.projects[]` 带这两个字段（需要 `discover_sync_service` 能力），`compose_inspect_result` 不带：

| 字段 | 取值 |
| --- | --- |
| `syncService` | 每个项目都有：拉取编码（字符串），读不出来为 `null`。compose 文件本身解析失败（带 `error`）的项目也照样带 |
| `syncConfigError` | 只在 `syncService` 为 `null` 时出现：`missing`（目录里没有这个文件）或 `invalid`（有文件，但读不出可用的编码）；读出编码时没有这个字段 |

- 编码取 `service`，它为空（缺失、`null`、空串）时取旧字段 `appName`，与 `docker/nocobase/sync-plugins.js` 的 `CONFIG.service || CONFIG.appName` 一致。必须是长度 1～255 的字符串，原样返回，不去首尾空白。
- 判为 `invalid` 的情形：不是普通文件（目录、命名管道等）；超过 65536 字节；不是 UTF-8 编码的 JSON 对象（空文件、带 BOM、数组、`null` 都算）；嵌套层数过深；取到的编码不是长度 1～255 的字符串；读取出错（如没有权限）；文件的真实路径（解析符号链接之后）不在 `PROJECTS_ROOT` 内。
- 悬空的符号链接：目标在 `PROJECTS_ROOT` 内但不存在为 `missing`，目标在根外为 `invalid`。
- 只读 `PROJECTS_ROOT` 内的真实路径：符号链接先解析再判断，越界的不打开；大小在读内容之前判断，超限的一个字节都不读。日志只记目录和错误类别，不记文件内容。
- 一个项目读不出编码，只是它自己带 `syncConfigError`，不影响其他项目，也不影响整次扫描（不会因此回 `io_error`）。

## 10. 运维口令校验（ops_code_*）

业务服务（`@orchisky/plugin-ops-guard` 的 hub 模式）在放行备份还原、迁移等受控操作前，经本机 Agent 请 Hub 校验运维口令。口令按环境（namespace）区分，由 Hub 的动态码服务按环境派生。

**本机端点** `POST http://<Agent>:18081/verifyOpsCode`

- 鉴权：请求头 `X-Agent-Secret`，与其他本机端点（`/queryPlugin`、`/pluginSyncChanged`、`/envConfig`、`/instanceSnapshot`）相同（恒时比较）。`AGENT_LOCAL_SECRET` 未配置时 403，不符时 401。
- 请求体：JSON，不超过 4KB，不合法返回 400。长度一律按字符计。

  | 字段 | 要求 |
  | --- | --- |
  | `service` | 服务编码，1～255 个字符 |
  | `code` | 口令，1～64 个字符 |
  | `operation` | 受控操作标识，匹配 `^[A-Za-z0-9_.-]{1,64}:[A-Za-z0-9_.-]{1,64}$` |
  | `operationLabel` | 可选，操作名，不超过 64 个字符 |
  | `operator` | `{userId, name, ip}`：userId 为非负整数或 1～64 个字符的字符串；name 不超过 128 个字符，ip 不超过 64 个字符，可省略 |
  | `file` | 可选，`{name, size}`：name 不超过 255 个字符，size 为非负整数 |
  | `target` | 可选，操作对象（备份文件名、应用记录号等），不超过 255 个字符；只供 Hub 记审计，不参与校验 |

- 响应：

  | 情况 | 状态码 | body |
  | --- | --- | --- |
  | 没连上 Hub；或刚连上、Agent 还在补投出站队列 | 503 | `{"reason": "unreachable"}`，可重试 |
  | 已连上、还没收到 `hub_hello`，且距连上不足 5 秒 | 503 | `{"reason": "negotiating"}`，可重试 |
  | `hub_hello` 不含 `ops_code`，或连上 5 秒后仍没收到 `hub_hello`（旧 Hub） | 501 | 无 |
  | 在途请求已达 8 个，或发送队列已满 | 503 | `{"reason": "busy"}` |
  | 等结果期间连接断开，或帧发送失败 | 503 | `{"reason": "disconnected"}` |
  | 8 秒内没收到结果 | 504 | 无 |
  | 收到结果 | 200 | 结果帧去掉 `type`、`requestId` |

  「连上」以 Agent 补投完出站队列、登记这条连接为准（补投没有条数和时长上限）：补投期间进来的请求得到 `unreachable`，5 秒的协商窗口也从补投结束时起算，免得补投久了把新 Hub 误判成旧版（501）。

**帧**

```json
{ "type": "ops_code_verify", "requestId": "…", "service": "done-admin", "code": "123456",
  "operation": "backups:upload", "operationLabel": "上传备份并还原",
  "operator": { "userId": 1, "name": "张三", "ip": "172.18.0.1" },
  "file": { "name": "a.nbdata", "size": 1048576 }, "target": "a.nbdata" }
{ "type": "ops_code_verify_result", "requestId": "…", "valid": true, "namespace": { "code": "prod", "name": "生产" } }
{ "type": "ops_code_verify_result", "requestId": "…", "valid": false, "reason": "locked", "retryAfter": 540 }
```

`valid: false` 时的 `reason`：

| 值 | 含义 |
| --- | --- |
| `invalid` | 口令错误或已过期 |
| `not_found` | 拉取编码对应的应用不存在、它所在的环境没有绑定这台 Agent 所在的服务器，或这台服务器上没有该应用的部署 |
| `locked` | 该（环境, 服务）失败过多被临时锁定，`retryAfter` 为剩余秒数 |
| `busy` | Hub 上该（环境, 服务）在途校验过多，可稍后重试 |
| `code_service_disabled` | Hub 未启用动态码服务，或其版本不支持按环境校验 |
| `unavailable` | Hub 内部出错 |

规则：

- Hub 在 `hub_hello.features` 里声明 `ops_code` 才支持本协议。Agent 的特性状态跟着连接走：连上时置为未知，收到 `hub_hello` 时记下，断开时清空。
- 结果帧不进出站队列：一问一答，断连即失败。Agent 断开连接时立即唤醒全部在途请求（返回 `disconnected`），迟到的结果按连接代次丢弃；结果和断线先到的说了算，已经收到的结果不会被随后的断线改写；连接被换掉而没有收到关闭事件时，旧连接上的等待者同样被唤醒。Hub 只把结果发回受理请求的那条连接，那条连接已断开就丢弃。
- Agent 只有一个发送线程，配容量 8 的有界队列；排队超过 8 秒、属于旧连接或调用方已放弃的帧直接丢弃。传输层与第 11、12 节共用（`core/hub_channel.py`），各持一个独立的通道，在途名额和发送队列互不占用。
- Hub 只信连接锁定的 Agent，不信请求里自报的环境：按 `service`（拉取编码）找到应用并以它所在的环境为准，核对这个环境已绑定该 Agent 所在的服务器、且这台服务器上有该应用的实例，再按（环境, `service`）分桶限流（10 分钟失败 20 次锁 15 分钟）。
- 两端日志都不记口令。

## 11. 环境开关（env_config_*）

应用（业务服务的每个节点）经本机 Agent 向 Hub 查所在环境的两个配置开关：`allowUi`（允许界面配置）和 `allowDs`（允许数据源配置）。应用侧每分钟查一次，Hub 里改了开关最多一分钟后生效。Agent 只转发，不缓存、不解读。

**本机端点** `GET http://<Agent>:18081/envConfig?service=<拉取编码>`

- 鉴权：请求头 `X-Agent-Secret`，与第 10 节的端点相同（恒时比较）：`AGENT_LOCAL_SECRET` 未配置 403，不符 401。
- `service`（拉取编码）必填，1～255 个字符，按字符计，不是字节；查询串里重复出现时取第一个。
- 路径精确匹配：只有 `?` 之前恰好是 `/envConfig` 才算，`/envConfigs`、`/envConfig/` 一律 404。
- 总时限 8 秒，包含等 `hub_hello` 协商的时间（见下）。应用侧的 HTTP 超时要大于它（应用用 10 秒），Hub 慢时应用拿到的是 Agent 的 504，而不是自己先超时。
- 成功：200，`Content-Type: application/json`，响应体就是开关对象本身，不再套一层：`{"allowUi": true, "allowDs": false}`。Hub 在 `env` 里多给的字段原样转给应用，Agent 只要求它是 JSON 对象，应用只取那两个布尔值。

**帧**

```json
{ "type": "env_config_query", "requestId": "…", "service": "k3f9qm" }
{ "type": "env_config_result", "requestId": "…", "env": { "allowUi": true, "allowDs": false } }
{ "type": "env_config_result", "requestId": "…", "error": "not_bound" }
```

`error` 为 `not_found` / `not_bound` / `unavailable`，含义同第 6 节的同名错误。Hub 按 `service` 找到应用，校验它所在的环境已绑定发请求的 Agent 所在的服务器；这个查询不写插件拉取记录。

**本机端点的状态码**（`/envConfig` 和 `/instanceSnapshot` 共用，第 12 节同此表）

| 状态码 | body | 谁回的 | 情形 |
| --- | --- | --- | --- |
| 200 | 开关对象 / 实例快照对象本身 | Hub 的结果帧 | 结果帧带 `env`（环境开关）或 `snapshot`（实例快照），且是 JSON 对象 |
| 400 | 无 | Agent | `service` 缺失、为空或超过 255 个字符 |
| 401 | 无 | Agent | 请求头 `X-Agent-Secret` 不符或没带 |
| 403 | 无 | Agent | `AGENT_LOCAL_SECRET` 未配置（一律拒绝，不放行） |
| 403 | `{"reason": "not_bound"}` | Hub | 应用存在，但它所在的环境没有绑定这台服务器 |
| 404 | 无 | Agent | 路径不认；旧版 Agent 还没有这个端点时也是这样 |
| 404 | `{"reason": "not_found"}` | Hub | 找不到这个拉取编码的应用，或这台服务器上没有该应用的实例 |
| 501 | `{"reason": "hub_unsupported"}` | Agent | Hub 在 `hub_hello.features` 里没声明对应特性；或连上后 5 秒内始终没收到 `hub_hello`（旧版 Hub） |
| 502 | `{"reason": "unavailable"}` | Hub 或 Agent | Hub 回 `unavailable`（内部出错）；或结果帧不合规：类型和请求对不上、残缺、`env` / `snapshot` 不是对象、`error` 认不得（Hub 以后可能新增） |
| 503 | `{"reason": "hub_disconnected"}` | Agent | 没连上 Hub（包括刚连上、Agent 还在补投出站队列的几秒）；等结果期间连接断开，或请求帧发送失败 |
| 503 | `{"reason": "busy"}` | Agent | 在途请求已达 8 个（正在等协商的也算），或发送队列已满 |
| 504 | `{"reason": "timeout"}` | Agent | 总时限内没拿到结果 |

**契约（这两个端点）：Agent 自己拒绝的 400 / 401 / 403 / 404 一律不带响应体**，表里其余各行都带 `{"reason": …}`（Hub 经 Agent 回来的 403 / 404 / 502，以及 Agent 自己折出的 501 / 503 / 504）。应用侧靠「有没有 reason」区分是谁拒绝的：没有 reason 的 401 / 403 是 Agent 本机拒绝了密钥，没有 reason 的 404 是 Agent 版本旧、还没有这个端点，带 reason 的按 reason 处理。所以不要给 Agent 自己的拒绝加响应体，也不要给 Agent 折出的错误去掉 reason；要改须与应用侧（`@orchisky/plugin-ops-guard` 的 `classifyHubRpc`）同步。`/queryPlugin` 不适用「没有 reason 的 404 是 Agent 版本旧」：它的 404 / 502 来自 Hub，同样没有响应体（第 6 节），那里只有 403 `not_bound` 带 reason。

规则：

- Hub 在 `hub_hello.features` 里声明 `env_config`（实例快照是 `instance_snapshot`）才支持。Agent 的特性状态跟着连接走：连上时置为未知，收到 `hub_hello` 时记下，断开时清空（同第 10 节）。
- 协商期间要等：运维口令校验在 `hub_hello` 还没到时立即回 503 `negotiating`，这两个查询则等待，不立即回 501，免得应用把「刚连上」误认成「Hub 太旧」。窗口从连上算起最多 5 秒（「连上」的算法同第 10 节）：`hub_hello` 到了立即继续，到时仍没有才当旧版 Hub 回 501。总时限包含等协商的时间。
- 结果帧不进出站队列：一问一答，断连即失败。断开连接时立即唤醒全部在途请求（503 `hub_disconnected`）；迟到的结果、旧连接的结果、不认识的 `requestId` 一律丢弃，已经收到的结果不会被随后的断线改写。Hub 只把结果发回受理请求的那条连接。
- 结果帧必须和请求对得上：类型是请求对应的那一种（`env_config_result` 对 `env_config_query`，`instance_snapshot_result` 对 `instance_snapshot_query`），响应体字段（`env` / `snapshot`）是对象；否则按 502 `unavailable` 处理，日志只记字段名，不记内容。
- 在途上限 8 个（包括正在等协商的），超出直接 503 `busy`：Hub 慢或卡住时，应用每分钟的查询不会越积越多。发送线程配容量 8 的有界队列，不设排队超时，只丢属于旧连接、或调用方已放弃的帧（与第 10 节不同：这里的调用方都有总时限，固定的排队超时会误丢仍在等的帧）。
- `AGENT_LOCAL_SECRET` 一台机器一个，只能证明请求来自这台机器：同机的应用改一下 `service` 就能查别的应用的开关或快照，Hub 仍按 `service` 核对那个应用所在的环境已绑定这台服务器（`not_bound`）。第一期接受这种机器级互信，和 `/queryPlugin` 一样。
- Agent 的日志只记路径、拉取编码和状态码，不记密钥，也不记结果内容。

## 12. 实例快照（instance_snapshot_*）

应用（运维管理页）经本机 Agent 向 Hub 取「本应用的实例快照」：应用、所属项目和环境、两个配置开关，以及这个应用在各服务器上的全部实例（停掉的也列）和每个实例的插件状态。数据来自 Hub 已有的上报和发布记录；快照里没有口令、密钥这类敏感内容。

**本机端点** `GET http://<Agent>:18081/instanceSnapshot?service=<拉取编码>`

- 鉴权、`service` 校验、路径精确匹配、状态码和响应体都同第 11 节（共用同一张状态码表）。
- 总时限 10 秒，包含等协商的时间；应用侧的 HTTP 超时要大于它（应用用 12 秒）。
- 成功：200，响应体就是快照对象本身，不再套一层。Agent 不解读也不校验快照的内容，只要求 `snapshot` 是 JSON 对象并原样返回。

**帧**

```json
{ "type": "instance_snapshot_query", "requestId": "…", "service": "k3f9qm" }
{ "type": "instance_snapshot_result", "requestId": "…", "snapshot": { "schemaVersion": 1, "…": "…" } }
{ "type": "instance_snapshot_result", "requestId": "…", "error": "not_found" }
```

`error` 同第 11 节：`not_found` / `not_bound` / `unavailable`。

**快照的形状**以 Hub 侧 `plugin-hub/src/server/services/instance-snapshot.ts` 的 `buildInstanceSnapshot` 为准，应用侧只认 `schemaVersion === 1` 且 `instances` 为数组的快照。对照写法：

```ts
interface InstanceSnapshot {
  schemaVersion: 1;
  collectedAt: string; // ISO
  app: { code: string; name: string };
  project: { name: string };
  env: { name: string; type: 'dev' | 'test' | 'prod'; allowUi: boolean; allowDs: boolean };
  instances: Array<{
    deploymentId: number;
    name: string;
    container: string | null;
    containerId: string | null; // 上报里的容器 ID 前 12 位；应用侧据此认「本节点」（容器里的主机名默认就是它）
    server: string;
    state: string | null; // 上报的 docker 状态，停掉的也列
    startedAt: string | null;
    reportedAt: string | null;
    pullCode: string | null; // 实例同步回执里的 service，没有回执为 null
    opsGuard: 'hub' | 'local' | 'off' | 'misconfigured' | 'unreported';
    opsGuardReason: string | null;
    plugins: Array<{
      name: string;
      releaseVersion: string | null; // 交付中枢里这个应用当前发布的版本
      installedVersion: string | null; // agent 扫到的版本
      status: 'consistent' | 'pending_update' | 'pending_install' | 'local_only' | 'updated' | 'failed';
      fromVersion: string | null; // 只有 updated 有
      error: string | null; // 只有 failed 有
    }>;
    lastSync: { status: string; finishedAt: string | null; error: string | null } | null;
  }>;
}
```
