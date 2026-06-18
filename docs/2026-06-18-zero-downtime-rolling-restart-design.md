# 零中断滚动重启 设计（design / spec）v2

- 日期：2026-06-18（v2，已纳入多 agent 评审 confirmed 项）
- 状态：待评审（v2 已按评审修订；用户再扫一遍后进入 writing-plans）
- 涉及仓库：`services-monorepo`（service-agent / service-hub，**主体**）、`cnp`（`@orchisky/plugin-service-hub`，仅平台按钮/action）。**节点侧零代码改动**（重启按 containerId、不需要 composeDir，见 §4）。

> ### v2 修订说明（相对 v1）
> 经 6 视角多 agent 评审 + 对抗核验（49 finding / 35 confirmed），修订要点：
> - **重启改为 `docker restart <containerId>`**，彻底删掉 composeDir 全链路（解决 v1 的"重启整个 compose project 击穿零中断"H1 + "宿主路径 vs agent 容器 cwd 错配"H2）。
> - **hub 命令面鉴权**列为 BLOCKER 前置（B1）。
> - 补**最小存活保护**（健康实例<2 默认拒绝，H3）。
> - hub 的"等结果原语 + 结构化返回通道 + rolling task 落表"明确为**新增工程**，不再说"复用"（H4）；走独立 WS 消息类型 + 新表，不塞进现有 CommandModel。
> - **容器对号主键钉成"宿主发布端口"**，对不上号**显式报错不静默跳过**（H5）。
> - 纠正 drain 默认值（代码默认 **30**，非 v1 误写的 40）、明确"网关默认无 Retry/熔断兜底，drain≥cache.ttl 是唯一防线"（M1）。
> - 补失败中途态、hub 重启恢复、WS 断连补发、各超时具体值、shutdown 匿名可达验收（M2–M6）。

## 1. 背景与目标

让"多节点部署"真正起效的第一步：**在插件分发平台一键重启某服务的多个节点，全程不中断**。

已在 49 真网关测试床（orchidea-gateway → `lb://memory-share` → 2 个 NocoBase 节点）实证机制：
- ✅ 正确滚动（先排空再重启）：**0 / 2354** 请求失败
- ❌ 朴素重启（不排空直接 restart）：**38** 失败（502/503）

核心机制（已验证、照搬）：逐节点 `POST /api/k8s/shutdown`（`@orchisky/plugin-service-k8s` 提供：注销 nacos + readiness 翻 503 + drain 期间继续服务）→ 阻塞返回（此时网关 LB 缓存已过期、不再转发到它）→ 重启容器 → 等就绪回注册 → 才轮下一个。

**承重墙**：节点 drain 时长（`K8S_GRACEFUL_DRAIN_WAIT`，**代码默认 30s**）必须 **≥ 网关 LB 实例缓存 TTL（`spring.cloud.loadbalancer.cache.ttl=30s`）**。网关**对后端无主动健康检查**，注销后最长仍转发 30s；且网关**默认未挂 Retry/熔断过滤器**（`loadbalancer.retry.enabled=true` 只是启用可重试基础设施、不会换实例重发）——**`drain≥cache.ttl` 是唯一防线**。默认值 30=30 余量为 0，**部署必须显式设 `K8S_GRACEFUL_DRAIN_WAIT > 30`（建议 40~45）**，缩短前须先在网关配 Retry 并验证。

### 目标
- 平台一键触发某服务的零中断滚动重启；平台只当触发器。
- 复用已验证机制，不引入新风险。
- 适配真实拓扑（§2），不强求 hub 接入各客户 nacos。

### 非目标（本期）
- 自动回滚到旧镜像（失败即停即可）。
- 真·多机跨主机编排（留接口，本期只做同机多实例；§6）。
- 平台前端大改 / 平台重写（诉求 3，另议）。

## 2. 拓扑约束（决定设计形态）

生产是**中心化 hub + 各客户自带 nacos**：
- `service-hub`（Python）与 `nocobase-hub`（平台）**中心化部署**（当前 49）；各客户 `service-agent` 经 WebSocket **反连**中心 hub。
- 每个客户的 **services + 自己的 nacos 在客户机器**（例：58 的 mom-admin 注册到 58 的 `192.168.1.35:8848`，49 够不着）。

⇒ **hub 不能读各客户 nacos**；而 **agent 与它要管的节点、与本地 nacos 天然同机**。
**因此：服务发现下沉到 agent；hub 不碰 nacos，只做跨命令编排。**

## 3. 架构与职责

```
平台(按钮, 带 admin token)
  --POST /api/rolling-restart {agentId, serviceName}-->  hub(编排, 鉴权)
       --WS list-instances / graceful-restart(by containerId)-->  agent(发现+执行)
            --读本地 nacos(发现实例) + docker(对号 containerId)
            --HTTP 宿主IP:发布端口--> node(/api/k8s/shutdown, /api/health/ready)
```

| 层 | 职责 | nacos | 改动量 |
| --- | --- | --- | --- |
| node | **零改动**：照常注册本地 nacos（带宿主 IP+发布端口）+ 已有 `/api/k8s/shutdown`、`/api/health/ready` | 注册本地（已有） | 0 代码（仅部署配置） |
| agent | `list-instances`（查本地 nacos + docker 对号出 containerId）、`graceful-restart`（按 containerId 优雅重启）；新增本地 nacos 读 + HTTP 能力 | ✅ 只读本地 | 中（新命令 + 对号 + HTTP） |
| hub | `rolling-restart` 端点（**鉴权**）：list → 逐个 graceful-restart、按序、失败即停、最小存活保护、进度可查、任务落表 | ❌ | 大（新端点 + 等结果原语 + 结构化通道 + 任务表 + 鉴权） |
| 平台 | 一个按钮 + 一个 action：传 `{agentId, serviceName}`（带 admin token）、轮询进度 | ❌ | 小 |

**平台维护零目录/零节点映射**：节点由 agent 经"本地 nacos（serviceName）→ docker（对号 containerId）"发现；平台只给 `agentId`(=台账 namespace) + `serviceName`。

## 4. 各组件改动与契约

### 4.1 node：零代码改动（前提：部署配置到位）
- **重启按 containerId，不需要 composeDir**（agent 用 `docker restart <containerId>`，规避宿主路径 vs agent 容器 cwd 的坐标系问题）。⇒ `@orchisky/plugin-service-nacos` 无需改动。
- 节点照常注册本地 nacos，并已有 `/api/k8s/shutdown`、`/api/health/ready`（plugin-service-k8s）。
- **nacos 必须登记宿主可达地址**：`NACOS_SERVICE_IP=宿主IP`、`NACOS_SERVICE_PORT=宿主发布端口`（既是 agent 对号主键，也是 agent/网关访问 `/api` 的地址）。getLocalIpAddress 的"首个非 internal IPv4"只是 fallback，生产应显式设。
- `healthBaseUrl` = nacos 登记的 `宿主IP:发布端口`（agent 经此访问节点 `/api`；要求该端口已 publish）。
- 必须显式设 `K8S_GRACEFUL_DRAIN_WAIT > 30`（见 §1 承重墙）。
- host 网络模式：见 §6 已知限制（P1 需显式 node→容器提示，否则不支持）。

### 4.2 agent：`service-agent`（monorepo，Python）
当前是纯 `docker compose` 执行器（`subprocess`，无 docker SDK、不 inspect、不发 HTTP）。本期**新增**：本地 nacos 读、docker 对号、HTTP 调用（`requests` 已在依赖）。

**新 WS 消息类型 `list-instances`**（走 message `type` 路由，参照现有 `logs_*` 先例，不复用 update/restart 命令通道）：
- 入参 `{serviceName, requestId}`
- 行为：
  1. HTTP GET 本地 nacos `${ctx}/v1/ns/instance/list?serviceName=&namespaceId=&groupName=`（**注意 context-path：本仓库实现一律带 `/nacos` 前缀**，照写裸 `/v1` 在标准部署会 404→静默空集）→ 取 `data.hosts` 并按 `healthy && enabled` 自行过滤
  2. 用 docker CLI（`docker ps --format '{{json .}}'` + `docker inspect` 读 `NetworkSettings.Ports`、`Config.Labels`，或新增 docker SDK——二选一须在实现期定）把每个实例**对号到本机容器**：**主键 = 宿主发布端口（HostPort）匹配**；容器 bridge IP 仅作兜底（宿主 IP 形态下 bridge IP 匹配会失效，不能与端口并列为"或"）
  3. 对号成功 → 取 `containerId`；**对不上号的实例不静默跳过**，在返回里显式标 `matched:false`（区分"在别机"与"匹配失败"——前者正常、后者由 hub 告警/拒绝）
- 返回 `[{address:"ip:port", containerId, healthy, matched}]`

**新 WS 消息类型 `graceful-restart`**（单节点优雅重启原语）：
- 入参 `{containerId, healthBaseUrl, settleSec, shutdownTimeoutSec, readyTimeoutSec, requestId}`
- 行为：`POST {healthBaseUrl}/api/k8s/shutdown`（HTTP 超时 `shutdownTimeoutSec` > 节点 drain）→ `docker restart <containerId>` → 轮询 `{healthBaseUrl}/api/health/ready==200`（总超时 `readyTimeoutSec`，覆盖冷启动）→ `sleep settleSec`（等网关纳回 LB）→ 回结构化结果
- 结果通道：新增**结构化返回**（result 带 `data` 字段或新 result message type），承载 list 数组 / 各步状态——现有 `output/message/error` 三字符串字段不够
- **断连补发（幂等）**：长命令（~90s+）期间 WS 抖动，agent 发结果时取"当前活动连接"，或本地缓存结果、重连后按 `requestId` 幂等补发（hub 侧按 requestId upsert）
- 失败语义：任一步失败回 failed + 输出，不自动回滚

既有 `update` / `restart` 不变。

**agent 新增 env**：`NACOS_SERVER`、`NACOS_NAMESPACE`、`NACOS_GROUP`、`NACOS_CONTEXT_PATH`（默认 `/nacos`）、`NACOS_USERNAME`/`NACOS_PASSWORD`（本地 nacos 开鉴权时；勿把含凭证 URL 打日志）。

### 4.3 hub：`service-hub`（monorepo，Python FastAPI）
hub 原治理"不做编排"，本期按用户决定**扩展**承载滚动编排（仍不碰 nacos）。以下均为**新增工程**（非"复用"）：

- **端点（均加 `_require_admin_token`，与 provision/rotate 一致）**：
  - `POST /api/rolling-restart` 入参 `{agentId, serviceName, settleSec?, force?}` → 返回 `{taskId}`
  - `GET /api/rolling-restart/{taskId}` → 整体状态（task 级 `running/done/failed/interrupted/degraded`）+ 每节点 `pending/in-progress/done/failed`（节点级 `unknown` 与细粒度 `draining/restarting/ready` 属 P1.5，见 §7）
- **编排流程**：
  1. 下发 `list-instances{serviceName}` → 等结果
  2. 有 `matched:false` 实例 → **报错/告警**（不静默继续）
  3. **最小存活保护**：健康实例数 `< 2` → 默认拒绝（提示"单实例无法零中断，需扩容或显式 `force`"）；`force` 时继续但进度/审计标 `degraded`
  4. **逐个**实例：下发 `graceful-restart{containerId, healthBaseUrl, ...}` → **等该命令出结果**（带总超时）→ 下一个
  5. **失败即停**：任一节点 failed/超时 → 停、不动后续、整体 failed
- **新增承重件**：
  - **等结果原语**：现 `dispatch` 发完即 202、result 异步落库；需新增"按 requestId 等待结果 future（带超时）"
  - **结构化返回通道**：见 §4.2（result 加 `data` 或新 message type）
  - **rolling-task 落表（本期必做，非开放项）**：记录 task + 每节点 requestId/状态/时间戳，作为进度契约 `GET .../{taskId}` 的数据源，并支撑 hub 重启恢复
  - **并发保护**：按 `(agentId, serviceName)` 用 **DB 唯一约束**（非内存 dict）防并发；与 agent 侧目录锁正交（agent 锁不负责跨实例顺序，顺序唯一保证在 hub 串行）
  - **hub 重启恢复**：启动扫描 `processing` 中的 rolling task → 标 `interrupted`，**拒绝新滚动直到人工确认**（不默默放行，避免与在途命令叠加致健康数失控）
  - **命令级超时/reaper**：现仅有 `heartbeat_timeout=90`（判 agent 在线，与命令完成无关，且有把重启中 agent 误判 offline 的隐患）；需为 rolling 命令单独设完成超时
- **审计**：不复用现 `commands/events`（`target_dir` 非空 + action 限 `update/restart`，新命令会 422/NOT NULL）；rolling task 表自带审计字段。

### 4.4 平台：`@orchisky/plugin-service-hub`（cnp）
- `serviceHubClient` feign 增加调 hub `rolling-restart` + 查进度（带 `X-Admin-Token`，与现有 feign 一致）。
- 一个 action（挂 service 资源，`loggedIn`，校验 serviceName 非空）：取选中 `agentId(=namespaceCode)` + `serviceName` → 调 hub → 返回 taskId；前端轮询进度。
- 一个按钮（client 端当前空壳，需补这一处最小 UI）。
- 边界说明：rolling-restart 的 authz 由 **hub 服务端**强制（校验 admin token）；平台带 token 只是满足校验、非边界本身。

### 4.5 向后兼容（现有"更新镜像"不破坏）
本设计**纯增量**，不改现有 `update`/`restart` 命令、`CommandModel`、agent compose 执行、平台 `t_comment_record:execute` 分发链路；新增能力走**独立 WS 消息类型 + 新 rolling-task 表**。
- 唯一触及现有链路的是 B1（给 `dispatch_command` 补 `_require_admin_token`）。**已确认安全**：平台 feign `serviceHubClient.sendRequest` 对所有调用（含下发命令 `AGENT_COMMENT`）强制带 `X-Admin-Token`、`ADMIN_TOKEN` 空则直接拒（`serviceHubClient.ts:88-105`）。故补鉴权只挡"未带 token 的非法调用"，不影响平台正常更新镜像。
- agent 端新增按 message `type` 路由的分支（`list-instances`/`graceful-restart`），现有 `command`(update/restart) 分支不动（`ws_client.py:_on_message`）。
- 实现期前置确认：nocobase-hub 已配 `ADMIN_TOKEN` 且与 service-hub 一致（现状已配，否则现有下发本就用不了）；rolling 的命令超时/reaper 须**仅作用于 rolling 命令**，不得误伤 `update`（大镜像 pull 可能耗时长）。

## 5. 配置 / env 一览

| 位置 | env | 默认/建议 | 作用 |
| --- | --- | --- | --- |
| node | `K8S_GRACEFUL_DRAIN_WAIT` | 代码默认 **30**；**部署必设 >30（建议 40~45）** | 真正的 drain；**必须 ≥ 网关 LB 缓存 30s**（默认值不安全） |
| node | `NACOS_SERVICE_IP` / `NACOS_SERVICE_PORT` | 宿主IP / 宿主发布端口 | agent 对号主键 + `/api` 可达地址（必设，勿依赖 fallback） |
| agent | `NACOS_SERVER` / `NACOS_NAMESPACE` / `NACOS_GROUP` | — | agent 读本地 nacos |
| agent | `NACOS_CONTEXT_PATH` | `/nacos` | nacos REST 前缀（裸 `/v1` 会 404） |
| agent | `NACOS_USERNAME` / `NACOS_PASSWORD` | 可选 | 本地 nacos 开鉴权时 |
| hub | `ROLLING_SETTLE_SEC` | 35（**必须 ≥ 网关缓存窗口 30**） | 单节点就绪后等网关纳回 LB |
| hub | `ROLLING_SHUTDOWN_TIMEOUT` | 60（> drain） | shutdown HTTP 超时 |
| hub | `ROLLING_READY_TIMEOUT` | 120~180（覆盖冷启动） | readiness 轮询总超时 |
| hub | `ROLLING_CMD_TIMEOUT` | = shutdown+restart+ready+settle + 余量 | hub 等单条命令结果总超时；须 ≥ agent 实际可能耗时 |
| hub | `ADMIN_TOKEN` | （已存在） | 命令面鉴权 |
| node(可选兜底) | `NODE_COMPOSE_DIR` / 容器名提示 | — | 仅 host 网络等对不上号时显式指定目标 |
| 网关侧关联(只读) | `loadbalancer.cache.ttl` / `nacos.discovery.watch-delay` / `loadbalancer.retry.enabled` | 30s / 5s / true | 决定 drain/settle 取值；retry 不换实例、非兜底 |

全部可改；drain/settle 不写死、且默认值不安全须显式配。

## 6. 分期（phasing）

- **P1（本期，覆盖测试床 + 58 现实形态）**：同机多实例。一个 agent 管本机该服务的实例；**每个实例 = 独立容器，按宿主发布端口对号出 containerId，按 containerId 重启**；hub 对这一个 agent 串 `list-instances` + 逐个 `graceful-restart`。
  - **已知限制**：host 网络模式下无发布端口/bridge IP，端口对号失效，P1 需节点显式提供容器名/目录提示（`NODE_COMPOSE_DIR` 或容器名 env），否则该形态不支持。
- **P2（留接口，后续）**：真·多机。hub 接受/聚合多个 agent，**跨 agent 也按序**（一台主机一台来，不让两台同时排空）。本机过滤由 §4.2 docker 对号天然实现（对不上号=非本机），无需额外 metadata。

## 7. 失败处理（本期）

- **失败即停**：任一节点优雅重启失败/超时 → hub 立即停，不动后续，task=failed，进度可见停在哪个节点。
- **失败节点中途态**：graceful-restart 内顺序是 shutdown(摘 nacos+翻 503)→restart→readiness 轮询；若 restart 后起不来，nacos 不会自动重注册（注册在 afterStart），该节点**已脱离 nacos 且未就绪 = 彻底掉线**。进度须标"需人工处理"，并输出该节点 compose/restart 输出 + 当前该 service 健康实例数。
- **最小存活联动**（与 §4.3 step3）：失败后若健康数跌到 0/1 须显著告警。
- **WS 断连**（**P1 靠超时兜底；精细处理为 P1.5**）：P1 阶段滚动中途 agent 断连，靠 `ROLLING_CMD_TIMEOUT` 超时把该命令判 `failed` 并停止（语义=failed，非 unknown）。P1.5 增强：① agent 按 requestId 幂等补发结果；② hub 侧 disconnect 时把该 agent 在途 rolling 命令标 `unknown` 并停（不乐观当成功、不简单超时误判）。⚠️ 因此 P1 务必让 `ROLLING_CMD_TIMEOUT` 既覆盖正常最坏耗时、又不至于让断连等待过久。
- **并发 / hub 重启**：DB 唯一约束防并发；hub 重启扫描中断的 task 标 `interrupted`、拒绝新滚动直到人工确认（§4.3）。
- 不自动回滚（旧镜像回滚后续再议）。

## 8. 安全

- **[BLOCKER] hub 命令面鉴权**：`dispatch_command`（commands.py:29-74）**现无 `_require_admin_token`**，且 `update` 接受任意 image+dir = 匿名换镜像/RCE。要求：
  - 新 `rolling-restart` + 进度查询加 `_require_admin_token`；
  - **同时**给现有 `dispatch_command`/`retry` 补 token（否则新端点加锁、旧端点仍是后门，等于没锁）——列入本期前置；
  - hub HTTP 端口**仅内网/仅平台可达**，不得随 `0.0.0.0` 对公网开放。**鉴权 + 网络隔离两者都要**。
- **shutdown 匿名可达**：`/api/k8s/shutdown` 中间件在 ACL/auth 之前、插件内无来源限制；是否匿名可达取决于部署期网关路由是否挂鉴权（代码保证不了）。要求：部署期验证 `curl -X POST https://<域名>/api/k8s/shutdown` 必须被拒（401/403，由网关鉴权挡下）。代码侧伴随项——给 shutdown 中间件加来源限制（仅本机/内网网段，或要求 `K8S_SHUTDOWN_TOKEN`），不只依赖可漂移的网关配置；**该代码加固本期 P1 三份计划未含**（会动 plugin-service-k8s、与"节点零代码"相悖），P1 先靠部署兜底，代码加固延后到 P1.5 单列。
- **目标校验**：agent 对最终重启目标做受管校验（containerId 来自本机 `list-instances`；若走 `NODE_COMPOSE_DIR` 兜底，强制 `os.path.commonpath` 落在受管根内）；平台透传的 `dir`/`serviceName` 非空校验。
- **既有基线（范围外但记录）**：agent 持 docker.sock = 宿主 root；`ADMIN_TOKEN` 是单一静态共享密钥，注意分发/泄露应对。这些先于本设计存在，本设计未实质扩大攻击面，但应在文档基线说明。

## 9. 验收

- 测试床已就绪并保留（49：memory-share-1/2 + memory-share-gw 网关 + dev 命名空间）。
- 功能验收：经平台按钮触发 → 滚动期间持续压网关 `/api`，**non-200 = 0**；对照（不排空）应见失败。
- **回归验收（必过）**：现有更新镜像功能不退化——平台下发 `update`（换 image）经 hub→agent 正常 `pull`+`down`+`up -d`，`restart` 正常；补鉴权后平台正常路径（带 token）仍通、裸调（不带 token）应 403。
- 安全验收：① 对 hub `rolling-restart`/`dispatch` 不带 token 应 **403**（`_require_admin_token`，未配置时 503）；② 对生产网关 `POST /api/k8s/shutdown` 匿名应被拒（401/403）。
- 边界验收：① 单实例服务触发 → 默认被拒（非静默中断）；② 制造一个对不上号实例 → hub 报错而非"成功漏滚"；③ 目标服务路由若配了 Retry/CircuitBreaker（per-route `wait-duration-in-open-state` 等）须单独覆盖验收。

## 10. 开放项（仅剩 UI / 取舍）

- 平台触发入口挂 service 资源（已定）下，按钮/进度展示形态：先 taskId + 手动刷新，还是实时进度条。
- `list-instances` 查 nacos **超时/不可达 vs 真空集**的区分：建议 nacos 报错 → task=failed 不滚；真空集 → 明确返回 `noInstances`（不算成功），避免把 nacos 抖动误判成空集"滚了个寂寞"还报成功。
- （落表 vs 内存 已定为**落表**，移出开放项。）
