# 插件分发改造方案(worker 无凭据 · agent 本机中转缓存 · 拓扑自动发现)

> 目标:把"服务插件分发"从「每个 worker 各自持密钥直连平台」改成「worker 零凭据 → 本机 agent →(持 token)平台」,
> agent 兼做**每主机插件缓存** + **拓扑自动发现**;平台只维护「服务→插件→版本」,不再手配节点/目录。
> 方案 = 上文选定的 **A(agent 直连 platform)**,且**包字节走 HTTP 不走 WS**。本文是完整方案,非最小改造。

> 🔺 **覆盖性声明(2026-06-22 决策,服务拓扑以此 + 计划 M 阶段为准,优先于下文各 §)**:
> 1. **hub + platform 已合并为单一 `service-console`**(一服务一 DB)。下文凡「platform」「hub」「platform → hub(WS)」措辞,一律读作 **service-console 内部**;原 hub 的 agent-WS / rolling / logs 路由是 console 进程内模块,**控制链由「跨进程 WS 调用」退化为「进程内直调」**,`hub_client` + 内部 `HUB_ADMIN_TOKEN`/`SERVICE_HUB_URL` 删除。agent↔console 仍是 WS(控制)+ HTTP(分发回源)。
> 2. **失败收敛 mode = freeze**:滚动中某实例失败 → 失败即停 + 标记「停在第 N 个」+ 人工重试/回滚,**不自动回滚**(故无需 per-实例「上一 deployed 版本」字段)。下文 §4.1 G5 / §8 `Rollout` 按此读。
> 3. **跨机服务(同 `nacos_service_name` 跨多 ns)发布 = 按 `nacos_service_name` 事务扇出置活**(单一真相的强模型):一次 publish 找出所有 ns-Service 行事务性同步置活(任一失败整体回滚 active),对账按 `nacos_service_name` 检 N 行不一致告警。**据此消解下文 §4「平台 active 版本是单一真相」与 §4.1/M12「跨多 ns」的措辞冲突**——active 仍 per-(ns,service) 行存储,但 publish 以 nacos 名为单位事务扇出,逻辑上对外即单一真相。
> 4. dir/镜像/容器**寻址权威 = DiscoveredNode**(§3.3 已述,推翻 v3);node-operations 下发链(`nodes.py`)须随之从 `Service.dir` 迁到 `DiscoveredNode`(见计划 P3-6)。

## 0. 设计原则

1. **worker 零凭据**:worker 只跟本机 agent 说话,不持任何 token / URL 密钥。
2. **agent 一台一个,持本 namespace 的 pull-token**(`namespace.code = agentId` 已是 1:1)。
3. **分发链与控制链解耦**:
   - 分发链:`worker →(本机 HTTP,无 token)→ agent →(HTTPS,pull-token)→ platform`
   - 控制链:`platform → hub →(WS)→ agent → docker`(start/stop/restart/redeploy)
   - 两链只在「发版投放」处汇合:发布新版本 → 控制链滚动重启 → 各实例经分发链重拉。
4. **最大化复用现有契约**:平台 `/api/distribution/*` 的返回 `[{pluginName, version, url}]` 是为现有 `sync-plugins.js` 量身定的(字段字面不改名)。worker 端解析逻辑零改动,只改 base URL + 去 token + 加兜底。
5. **不发明新坑**:包(.tgz)只走 HTTP;不过 agent↔hub 的 JSON/WS 通道。

## 1. 目标架构

```
                          ┌──────────────── 控制面(all-in-one console / 或分离)────────────────┐
                          │   platform(BFF + 分发端点 + 台账)   ⇄ 内网 ⇄   hub(agent broker)  │
                          └──▲───────────────▲──────────────────────────────────▲──────────────┘
              pull-token │   │ 分发(HTTPS)  │ 发现上报(WS)        命令下发(WS)│ agentKey
                         │   │               │                                    │
   ┌─────────────────────┼───┼───────────────┼────────────────────────────────────┼──────────┐  每台服务器
   │  agent(一台一个)   │   │               │                                    │          │
   │   ├─ plugin-cache(本机 .tgz 缓存,回源平台)◀────────────────────────────────┘          │
   │   ├─ worker-facing HTTP:/plugins /download(无 token)                                     │
   │   ├─ discovery:nacos + docker labels → 周期上报拓扑                                       │
   │   └─ executor:docker compose(受控于控制链)                                              │
   │        ▲ 无 token,本机 HTTP                                                               │
   │  ┌─────┴──────┐   ┌────────────┐                                                          │
   │  │ worker A   │   │ worker B   │  (同 service 多容器:admin / 2admin)                    │
   │  │ sync-plugins│  │ sync-plugins│                                                          │
   │  └────────────┘   └────────────┘                                                          │
   └───────────────────────────────────────────────────────────────────────────────────────────┘
```

组件职责:

| 组件 | 职责 | 持有的凭据 |
| --- | --- | --- |
| worker | 启动时向本机 agent 要插件清单 + 下载包 + 安装 | **无** |
| agent(一台一个) | ① 本机插件缓存 + 回源平台 ② worker-facing HTTP ③ 拓扑发现上报 ④ 执行 docker 命令 | 本 namespace 的 **pull-token**(分发)+ **agentKey**(连 hub) |
| service-console(原 platform + hub 进程内合并) | 维护「服务→插件→版本」、提供 `/api/distribution/*`、节点台账 UI、发布;内置 hub 模块(`app/hub/`)做 agent broker:命令下发 + 接收发现上报 + 节点心跳台账 | —(进程内,无 platform→hub token) |

## 2. 分发链详设(worker ↔ agent ↔ platform)

### 2.1 worker → agent 契约(新,本机、无 token)

agent 在本机 HTTP(扩展现有 `core/health_server.py` 的 `ThreadingHTTPServer`,或新起一个 `PLUGIN_SERVE_PORT`)上暴露:

```
GET http://<agent-host>:<port>/plugins?service=<serviceCode>
  → 200 [{ "pluginName": "...", "version": "...", "url": "http://<agent-host>:<port>/download/<attachmentId>" }]
  (worker 无 namespace 概念 —— 即便配置里带了 namespace,agent 也**忽略其值、恒用自己配置的本 ns**;worker 无法越 ns 拉别的 namespace 插件;url 指向 agent 自己,worker 无需 token)

GET http://<agent-host>:<port>/download/<attachmentId>
  → 200 流式 .tgz(命中本机缓存直接给;未命中由 agent 回源平台后再给)
```

> **关键兼容**:返回的 `pluginName/version/url` 三字段与平台 `/api/distribution/plugins` 字面一致 → `sync-plugins.js` 的**解析与安装逻辑零改动**,只改「base URL 指向本机 agent」+「去掉 Authorization」。

### 2.2 agent → platform 回源(持 pull-token)

```
agent 拉清单:GET {PLATFORM_URL}/api/distribution/plugins?namespace=<本ns>&service=<svc>
              Authorization: Bearer <agent 的 pull-token>
            → [{pluginName, version, url=平台 download URL(含 attachmentId)}]
agent 改写:  把每条 url 改成指向「自己」/download/<attachmentId> 再回给 worker。
agent 下包:  /download/<attachmentId> 未命中缓存 → GET {PLATFORM_URL}/api/distribution/download/<id>
              Authorization: Bearer <pull-token> → 落缓存 → 回 worker。
```

平台侧**完全复用现有端点**(`distribution.py`):`query_plugins`(token 属 namespace 校验 + 写 fetch_record)、`download`(IDOR 归属链校验 + 流式 .tgz)。

### 2.3 缓存策略(agent 的红利)

- **内容寻址**:缓存键 = `attachmentId`(同 id = 同包),命中即用。
- **一台只回源一次**:同主机多容器同服务(admin/2admin)→ agent 拉一次,喂多个 worker。
- 容量上限 + LRU 淘汰;`PLUGIN_CACHE_DIR` 落盘,容器重建可挂卷保留。
- 可选完整性校验(若平台返回 sha256;现契约暂无 → 后续在 distribution 响应加 `sha256` 字段,向后兼容)。
- 并发同包用锁(借现有 `handlers._get_project_lock` 同款 per-key 锁)。

### 2.4 鉴权边界

- **worker → agent 无 token**:授权边界 = **agent 配置的本 namespace**。agent **忽略 worker 传入的 namespace、恒用自己配置的本 ns**——同主机容器无法越 ns 读别的 namespace 插件(这关掉了评审 M5「worker 透传 service/ns 越权读整个 namespace」的越 ns 面)。namespace ↔ agentId 名义 1:1(`create namespace` 时 provision)。
- **同主机信任域**:同主机任意容器都能问 agent 要**本 ns**插件 —— 按内网信任域**接受**(评审 M5 的「同主机=同 ns 全插件共享信任域」+ 1:1 未代码强制,均按 §5「安全取舍」有意降级,不加部署摩擦)。源 IP→容器校验列为可选加固,不强制。
- **agent → platform**:pull-token 做 namespace 隔离 + 下载 IDOR 防护(平台已实现)。

### 2.5 审计(不丢调用方维度)

- **分层(评审 M2/M9 收口)** —— 不要承诺平台给出完整 per-container 视图:
  - **平台 `fetch_record` = 回源粒度审计**(ns/service 维度):agent 命中本机缓存**不回源**,平台只在「未命中那一次回源」记一行;同主机共享缓存的其余 N-1 个容器平台**不可见**。故平台审计语义降级为「哪个 ns/service 何时回了哪个版本的源」,**不是** per-container。
  - **per-container 审计以 agent 本地日志为权威**:agent 记「谁(源 IP→docker 反查的容器/工程)何时问我要了什么 + 命中/回源」;§7 给 agent 本地审计一条查询/上报路径。平台不可达时本机仍有审计。
  - **caller 维度只取 agent 可验证的事实**(源 IP→`docker inspect` 反查容器/工程),**不采信 worker 自报的 `X-Caller-Container`**——tokenless 本机请求方可任意伪造,写进权威审计等于放任伪造溯源(评审 M9)。如保留自报 header 仅作辅助提示,需在 UI/schema 标「未验证、不可作溯源依据」。

## 3. 发现链详设(agent 自动上报拓扑 —— 干掉手配)

### 3.1 agent 周期发现(已有积木:`nacos_client` + `instance_match` + `docker_cli`)

```
每 N 秒:
  nacos_client 查本 namespace/group 的服务实例
    → instance_match 过滤"本机"(ip:port 匹配)→ 得本机实例 + containerId
    → docker inspect 取 container_name、com.docker.compose.project、
       com.docker.compose.project.working_dir(=dir)、image
  ∪ docker 直接列本机受管 compose 工程(覆盖"已停、nacos 看不见"的节点)  ⚠️【P3 待建】现 docker_cli 仅 `docker ps -q`(running),需新增 `docker ps -a`/`compose ls -a`
  → 组装节点清单 [{nacosService, composeProject, containerName, dir, image, host, running, healthy}]
  → 经 WS 上报 hub
```

> **nacos 只见在跑的** → 用 **docker(含 stopped 容器/工程)为主**、nacos 补 `nacosService`/`health`。这样"已停服务"也能在台账里被管理(可被 start)。
> ⚠️ **诚实标注**:"含 stopped 为主"是发现链的**基石**,但当前 `docker_cli` 只有 `docker ps -q`(仅 running),该采集器**零实现**——"已停也可管"这条卖点是 **P3 待建**,不是已有积木。排期勿按"已有"估。
> ⚠️ **并发坑(评审 L2)**:周期发现与 compose 执行(down/up/pull)是同一 agent 上两条并发读 docker 的路径,无协调。发现恰在某工程 down→up 窗口采样 → 误报 `running=false`/不健康 → 台账抖动、§4.1 健康门可能被自家滚动造成的瞬时下线误判。**做法**:发现上报对「本机正在执行 compose 命令的工程」跳过或标 `transitioning`(复用 `_project_states`/`get_command_execution_state`);§4.1 健康门在滚动期间排除「本任务正在重启的实例地址」,只对其余算 quorum。

### 3.2 hub 接收 + 落库 + 心跳老化

- hub 新增一条 agent→hub 上报消息类型;落「发现节点台账」表;心跳 `heartbeatAt`(借 cnp `o_handler_registry` 模式)。
- **心跳处理 = 标记而非删除(评审 M8 收口)**:**agent 在线态与 DiscoveredNode 存活解耦**。agent 失联超 TTL → 把其名下节点标 `stale`/`unknown` 并**保留行**,**不**连带清除——否则「已停但仍想 start」的节点连同 dir/工程定位信息一起蒸发,恰在最需要人工介入时台账空了;只有**显式确认主机下线**才删行。(与 cnp「24h 无心跳清行」不同:这里节点行承载运维寻址信息,改为标记。)

### 3.3 platform 台账:手配 → 自动 upsert

- **节点(物理)与服务(逻辑)分表**(推荐):
  - `Service`(逻辑服务)= 插件分发的单位,key = (namespace, **service_code**)。**人工只配这层 + 它装哪些插件/版本。** `service_code`(分发标识)与 `nacos_service_name`(发现/滚动 link 用)是**两个独立列**:创建时默认填同一值、但**可分别编辑**,不是恒等(见 §3.4)。
  - `DiscoveredNode`(物理节点)= (agentId, composeProject/containerName, dir, image, host, heartbeatAt),**全自动 upsert**;**dir / 镜像 / 容器以此为权威**,供实例运维页 + 控制链寻址。
- **admin/2admin 自动落位**:两个 compose 工程 → 两行 DiscoveredNode(dir 各异);插件按 nacosService(同)→ 同插件集。两套粒度自动各就各位,不再像现在手配 node1/2/3 那样别扭。
  > ⚠️ **落位的坑(评审 M3)**:`instance_match` 以宿主发布端口为主键、bridge IP 兜底,**无 compose project 校验**(滚动路径才有 composeProject 漂移守卫)。同机两工程若 nacos 注册的是**容器内端口**(常见两工程都 13000)→ 端口主键命中失败落 IP 兜底,共用默认 bridge 网段时 IP 也可能撞 → DiscoveredNode 的 dir/composeProject 绑错工程,后续「更新/重部署」按错 dir 作用到另一组容器。**要点**:钉清 nacos 注册端口语义(宿主 vs 容器内);匹配成功后带 composeProject 一起上报、hub 校验唯一性;对「一容器被多实例匹配 / 一实例匹配到多容器」**做冲突检测告警,不静默取第一个**。
- namespace 仍由 platform `create namespace` 时 provision(1:1 绑 agent),show-once 返回 pull-token。

> 🔺 **本文推翻 node-control v3 §3 的「Service 表权威源」模型**(2026-06-21 定):v3 把 `Service.dir`/`default_image` 作为 compose 寻址权威、手填台账;本文改为 **dir/镜像由 `DiscoveredNode`(agent 自动发现)权威驱动控制链**——即"干掉手配"。安全闸**不新增部署强约束**(取舍:agent 内网、部署省事优先):沿用 agent 现有 `compose.py:validate_managed_dir`(执行 dir 的 realpath 须落在 `MANAGED_PROJECTS_ROOT` 下、拒 self-project)为兜底即可;`db_models.py` 的 `Service.dir`/`default_image` 列退化为可选展示 / 迁移期回退,不再作为控制链寻址来源。

> 📖 **术语表(评审 L11 收口,全文统一)**:
> - **实例** = `DiscoveredNode` = 一个被管 compose 容器(数据模型层叫 DiscoveredNode,UI/文案统一叫「实例」,侧栏也改,不再混用「节点」)。
> - **nacos 实例** = 服务注册视角的一条实例,正常与 DiscoveredNode **1:1**(若可能不 1:1 须显式说明)。
> - **`service_code`**(分发标识,`distribution.py` 查清单用)≠ **`nacos_service_name`**(发现/滚动 link 用);两列,默认同值可分别编辑。
> - 两个 `/plugins` 端点**不同路径**:worker→agent 用 **`/plugins`**(对内、无 token、无 ns 入参);agent→platform 回源用 **`/api/distribution/plugins`**(持 pull-token、带 ns)。
> - **namespace**:worker 侧无此概念(agent 恒用自己配置的本 ns,见 §2.1/§2.4)。

### 3.4 服务纳管模型(意图 vs 现实)—— 服务不纯自动建

- **物理节点(DiscoveredNode)= 纯自动**:dir / 镜像 / 容器全来自 agent 发现,无手配。
- **逻辑服务(Service,插件策略落点)= 不纯自动建**。纯自动建有三个硬伤:① 首次启动鸡生蛋(服务没配插件 → 拉空);② 服务停了不能删(策略要留)→ 垃圾累积;③ 任意 worker 注册即建 → 管控弱。改走:
  - **自动发现服务名 + 一键纳管 + 人工配插件**:发现的 `nacosService` 若 ∉ Service 表 → 进「**已发现未纳管**」收件箱;admin 点「纳管」→ 平台预填(namespace + nacosServiceName,零敲)→ 建 Service → 配插件。
  - **保留手动预建**:首次上线前想让 worker 一启动就有插件 → 提前手建(敲一次 nacos 名)绕开鸡生蛋。
  - Service 行**持久**(与是否在跑无关),服务停了**不自动删**(插件策略要留)。
- **对账视图(意图 `Service` ⋈ 现实 `DiscoveredNode`,by nacosServiceName)**:
  - `running-but-unmanaged`(在跑但没纳管)→ 提示纳管 + 配插件。
  - `managed-but-down`(纳管了但没实例)→ 该起没起。
  - `version-drift`(active 版本 ≠ 实例实际版本)→ 待投放。
- **link key(两列,不强制相等)**:发现节点经 **`nacos_service_name`** link 到逻辑服务(`DiscoveredNode.nacosService` ⋈ `Service.nacos_service_name`);分发链则用 **`service_code`**(`distribution.py` 按它查清单)。二者是 `db_models.py` 上两个独立 nullable 列、职责不同:创建表单默认把 `service_code` 预填成与 `nacos_service_name` 相同,但仍可分别编辑。**删除早先"serviceCode = nacosServiceName 恒等"的说法**——`nacos_service_name` 现 P1 允许为空,恒等会让分发链拿到空 key;两列不等时"对账 by nacos_service_name"也 link 不上。

## 4. 控制链 / 投放(复用现有 node-control,不重造)

- 发布新插件版本(platform 改 `service_plugin_version` 的 active) → **触发受影响 service 的滚动重启**(platform → hub `rolling-restart`,逐实例 drain+restart)→ 各实例启动 → worker 经本机 agent 重拉(agent 此时已预热缓存,秒级)。
- **版本一致性**:平台 active 版本是单一真相;滚动逐实例,期间新起实例都拉同一目标版本。
- **灰度**(可选增强):发布时只滚一部分实例 / 按 DiscoveredNode 子集滚。
- 发版前 agent 可**预热**:platform 通知 agent「新版本将至」→ agent 提前回源缓存 → 滚动时 worker 拉取零等待。

> ⚠️ **现状诚实标注(评审 M1)**:上面「发布 active → 自动触发滚动」是 **P4 待建**——**当前 `releases.py` 的 publish/rollback 只改 DB `active` 行、不触发任何控制链**(真正调控制链的只有 `nodes.py` 的人工对单 agent×service)。**P4 前的过渡语义**:改 active 后在跑 worker 不会自动重拉,UI 必须显式标「**待投放(version-drift)**」并给一键投放,别让用户误以为 publish 已生效。且「publish→滚动」自动触发**强依赖 §4.1 协调器**(无协调器时自动触发反而撞 §4.1 守卫失败)→ 必须**与 §4.1 同批、排在其后**,不可早于它分期。

### 4.1 ⚠️ 跨 agent(多节点)滚动的盲点 —— 必须 hub 协调

**场景**:两台服务器各一个 agent、共用一个 nacos,各跑一个同名服务 `tamagawa-wms`(该 nacos 服务有 2 个实例,分属两台)。

**当前实现的真实表现**(守卫在 **hub 编排器** `service-hub/app/routers/rolling.py:_run_rolling`,**不在 agent**):
- rolling 命令**定向单 agent**(`rolling_restart(agentId, serviceName)`),**不广播**——不会「两个 agent 同时收到同一条命令」。
- agent 的 `handle_list_instances` 逐实例只回 `matched: true/false`(**从不失败**);是 hub `_run_rolling` 拿到结果后,见到**非本机(unmatched)**实例就**整体 abort**(有测试 `test_run_rolling_unmatched_aborts` 直接驱动 hub 验证)。
- 所以给 agent A 发 rolling → hub 拿到含 B 的 unmatched 实例 → **整体 abort,滚不动**。

**结论**:当前 rolling 只支持「**同一台机多容器**」(admin/2admin),**不支持「一个服务跨多台机」**——而后者正是真正的多节点生产形态。「两个 agent 同时重启→有中断」是另一种 naive 修法(去守卫各滚各的 / 广播并行)的后果。

**正确做法:hub 跨 agent 协调的顺序滚动**:
1. hub 取该 nacos 服务全部实例,按 host→agent 分组(靠自动发现的「实例→agent」映射)。
2. **串行**滚:agent A 的实例(drain→重启→等 ready)→ agent B → …,全程维持集群健康 **≥1(或 ≥quorum)**,**绝不同时重启**。
3. 「≥2 健康」门从「单机内」升级为「**集群级**」不变式;失败即停。

**要改两处(均在 hub,非 agent)**:① hub `_run_rolling` 把「有 unmatched 就 abort」改成「**过滤掉 unmatched、只滚本机 matched**」(agent 无需改——它已逐实例回 `matched` 标志);② hub 加**跨 agent 顺序协调器**(已能定向单 agent,补一层 for-each-agent 串行 + 集群健康门)。**依赖自动发现**(hub 须知每个实例在哪个 agent),与「agent 上报拓扑」配套。

**跨机服务的数据建模(评审 M12)**:一个「跨多台机」的逻辑服务 = **同 `nacos_service_name` 在多个 agent/ns 上各有实例**(因 ns ↔ agent 1:1,跨机即跨 ns)。所以**滚动的聚合 key 是 `nacos_service_name`(可跨 ns)**,与 per-ns 的插件分发 `Service`(`service_code`)区分:hub 按 `nacos_service_name` 把各 agent 的 DiscoveredNode 聚到一起做顺序投放;三态对账对跨 agent 同名服务也要按 `nacos_service_name` 聚合健康/漂移。原型须补一个「同 nacosService 跨多 agent」样例服务,否则 §4.1 无从验收。

**落地前置与循环依赖(评审 L1)**:① hub 当前**没有**「nacos 服务 → 在哪些 agent 上有实例」的映射,这是**硬前置依赖 §3**(发现链);② 把守卫改成「只滚本机」后,健康不变式从单机升到**集群级**,hub 必须在滚 A 期间**持续重新探测** B 的健康(现健康判定只用单次快照,不够),且排除「本任务正在重启的实例」(见 §3.1 L2);③ §4 想让 publish 自动触发,前提就是本协调器 → 二者**同批上线、不分期**。

**失败即停后的收敛语义(评审 G5)** —— 必须定义,否则集群停在半迁移态无人收口:① **失败即停**后两种模式二选一并写清:**冻结半迁移态等人工** / **自动回滚已动实例到上一 deployed 版本**;② §8 增「**发布运行记录**」表(目标版本、实例序列、各实例状态、失败点)支撑**断点续投与回滚**;③ 原型进度视图加失败路径(某实例 drain 502 → 标红 → 「已停在第 N 个,前 N-1 个已新版/已回滚」+ 重试/回滚),让该不变式可被 UI 验收。

### 4.2 服务的两个管理面 + 平台信息架构(与原型对齐)

服务(逻辑)挂**两个独立管理、独立版本**的维度,各有独立投放方式:

| 管理面 | 管什么 | 操作(服务的二级页,编辑=暂存) | 发布机制(由 diff 自动选) |
| --- | --- | --- | --- |
| **插件配置** | 该服务装哪些插件 + 各插件 active 版本 | 绑定 / 解绑插件、改版本(选版本) | 滚动重启 → worker 经 agent 重拉插件(§2 / §4) |
| **镜像配置** | 该服务的基础镜像(nocobase-pro tag) | 发布新镜像、历史、**回滚** | pull-redeploy 逐节点(§4 / §4.1) |

- **编辑 = 暂存意图,发布 = 应用 diff(desired-state apply)**:两个面只改 Service(逻辑层)的「意图」(暂存、标『待发布』);**一个「发布」**算 diff 后**自动选机制**——**仅插件变 → 滚动 restart;含镜像变 → pull-redeploy**(逐节点 pull/down/up;新容器启动顺带重拉插件,故插件变更自动包含)。**不设两个并列的发布动作**(镜像发布 ⊃ 插件发布)。发布弹窗展示「变更摘要」(镜像 X→Y / 插件增改删)+ 将执行的机制。
- **创建服务**只填 `命名空间 + 服务编码(= nacos 服务名)`;**目录 / 镜像不在创建表单**——目录纯节点级(自动发现),镜像走「镜像配置」管理面。
- **镜像有历史 + 回滚**:每次发布镜像入历史,可一键回滚到旧 tag(pull-redeploy)。

> ⚠️ **pull-redeploy 也走 §4.1 协调(评审 M11)**:别把「含镜像变 → pull-redeploy」当已可用的原子动作。pull-redeploy **中断更久、更需跨 agent 串行保活**,与 restart **共用 §4.1 的跨 agent 顺序协调 + 集群健康门**,只是单节点动作从 graceful-restart 换成 drain→pull→up。§4.1 的结论应从「滚动重启」**泛化为「跨 agent 顺序投放(restart / pull-redeploy 通用)」**。

**信息架构(侧栏)**:
- **配置**:命名空间、服务、插件(全目录仓库)
- **发布**:插件上传(列表带「发布」= 选 ns + 服务)、获取记录(按 命名空间 + 服务 + 实例 聚合,插件@版本进详情)。〔原「插件发布」独立页已删——发布统一走服务的「发布」〕
- **运维**:实例、操作审计
- **服务的「配置」「镜像」是服务的二级页**(从服务行进入,服务**只读锁定**,带返回;不在侧栏)。
- 服务列表分 **已纳管 / 未纳管** 两标签页(纳管模型见 §3.4);**「实例」页**(原称「节点」,即 DiscoveredNode 的 UI 名)全自动发现(§3)。术语统一:命名空间 / 服务名(UI 去掉 (agent) / (nacos) 标注;底层 `service_code` 与 `nacos_service_name` 仍是两列、默认同值,见 §3.4)。
- **实例运维动作简化为「启动 / 停止 / 更新」**:`更新` = 把该实例拉到**服务期望态**(按 diff 自动选:仅插件变 → restart;含镜像变 → pull-redeploy),**取代分立的「重启 / 重部署」**——与 §4.2「一个发布按 diff 选机制」同一思路,只是作用域为单实例。

### 4.3 实例实时日志(agent + hub **已实现**,仅缺 platform UI)

> ⚠️ **更正(2026-06-22 核码)**:本节早先写「agent 是一次性命令模型、流式是全新能力、全链路要建」——**错**。核 `service-agent`/`service-hub` 实际代码后确认:**实时日志在 node-control 阶段已端到端实现**(agent 流式 + hub SSE + e2e),**只剩 platform 实例页 UI 待接**。协议以**既有代码为准**,不要另造。

**链路(现状)**:

```
浏览器/platform UI  ──→  hub SSE: POST /api/agents/{agentId}/logs/stream
                                      ⇅ (WS)
                          agent: docker compose logs -f --tail N  (按 compose 工程目录 dir)
```

**已有(node-control 阶段实现,勿重造)**:

- **agent · `core/log_sessions.py`**:`start_log_session` / `stop_log_session`;daemon 线程跑 `open_compose_process(dir, ["logs","-f","--tail",N, ("--timestamps")])`;WS 回 `logs_started` / `logs_chunk` / `logs_finished` / `logs_error`;入参 `sessionId` + `dir`(**compose 工程目录,非 containerName**) + `tail`(默认 200) + `timestamps`;停止/结束 `terminate → wait(5s) → kill`,**不留孤儿**;**已带 `validate_managed_dir` 安全闸**(realpath 须在受管根、拒 agent 自身,防越权读任意目录日志)。
- **hub · `app/routers/logs.py`**:`POST /api/agents/{agentId}/logs/stream` 返回 **SSE**(`text/event-stream`);**admin-token 鉴权 + `requested_by` 服务端据 token 派生**(客户端 `X-Requested-By` 仅 hint、不入审计,无旁路);agent 在线/连接校验;**多订阅 fan-out**(`subscribe_log_stream` → subscriberId + queue,多人看同实例共享一条 agent 流,**最后一个退订才向 agent 发 `logs_stop`**);响应头回 `X-Log-Session-Id`。有 e2e:`service-hub/scripts/validate_logs_stream_e2e.py`。

**仅待建(platform 侧)**:

- **platform 实例页日志查看器 UI**:打开 hub 的 SSE(平台 BFF 透传,或 SPA 直连 hub `/api/agents/{agentId}/logs/stream`),消费 `started/chunk/finished/error` 事件渲染;`dir` 取自该实例对应 `DiscoveredNode` 的 compose 工程目录、`agentId` 取自其所属 agent。
- 前端体验:tail 默认 200(代码默认,可配)、自动滚动、关闭/切走即断开 SSE(hub 的 finally 会向 agent 发 `logs_stop` 收口)。
- **不要**另造 `streamId / container / log-stream-*` 协议(早先设计的错误命名);一律用既有 `sessionId / dir / logs_*` + hub SSE 端点。

**权限**:复用既有 admin-token 受信链 + `validate_managed_dir`,内网从简(§5),不额外加固。

**现状对照 = ⚠️ 大部分已有**:agent 流式 + hub SSE + fan-out + 安全闸 + e2e 均已落地;**唯一待建 = platform 实例页 UI 接 hub SSE**(P3,小工作量)。

## 5. 安全模型(完整)

| 链 | 凭据 | 边界 |
| --- | --- | --- |
| worker → agent | **无** | 同主机信任 + agent namespace 隔离(P2 可加容器来源校验) |
| agent → platform 分发 | 本 namespace **pull-token**(可轮换,show-once) | token 属 namespace 校验 + download IDOR 归属链(平台已实现) |
| agent ↔ console 控制 | **agentKey**(WS) | console 内置 hub 模块校验 |
| platform↔hub(合并后进程内直调) | 无(原 admin token 链已删) | `app/hub_client.py` 进程内 await,无跨进程边界 |
| 优雅停机 | `K8S_SHUTDOWN_TOKEN`(X-Shutdown-Token) | cnp opt-in 闸(已合 1.7.x_v2) |

- **去掉老坑**:不再有「root 管理员 JWT(exp≈3026 年,等于永不过期)散落在每个 worker」。凭据只在 agent(一台一个,可轮换)。
- pull-token 分发:`create namespace` / `rotate-pull-token` show-once → 注入 agent 配置(部署密钥管理),worker 不碰。
- **agentKey 生命周期(评审 G6 收口)**:agentKey 是控制链凭据,须与 pull-token 同等定义清楚(原文只写了 pull-token)。**provision**:随 `create namespace` 一并 **show-once 返回 agentKey + pull-token**(二者都在创建时下发);**存储**:hub 侧存 agentKey **hash + 绑 agentId**;**校验**:agent↔hub 建 WS 时 hub 校验;**轮换**:`rotate-agentKey` 端点 + 下发链。(原型新建命名空间提示须**同时返回二者**,不可只给 agentKey——见评审 L13。)
- **传输层(修掉老 `rejectUnauthorized:false` 的 MITM 坑)**:
  - worker → agent:本机 **plaintext HTTP** 即可(不出主机、不上网,无需 TLS;agent 监听 `127.0.0.1` 或本机网段)。
  - agent → platform:走**正经 TLS**(有效证书或把 CA 加进 agent 信任库),**不要 `-k`/`rejectUnauthorized:false`**。老 sync-plugins 那套跳过校验只是为自签证书临时凑合,迁移时一并修掉。

> 🔻 **安全取舍(2026-06-21 定,优先级低于部署省事)**:**前提是 agent 是内网服务、不暴露外网,且部署简单是最高优先级**。基于此,ultracode 评审的下列"网络暴露/投毒/越权"类发现**整体有意降级、不提到 P0/P1**,记录在案而非静默忽略:
> - **H1**(agent 入站默认绑 `0.0.0.0`)、**L4**(同源,§5 文档说 127.0.0.1 但代码默认 0.0.0.0):内网不暴露,降级;零成本默认仍**建议** worker-facing 监听写 `127.0.0.1`。
> - **H2**(插件包全链路无 sha256):内网可信链路,降级;不加包校验摩擦。
> - **M5**(ns 1:1 未代码强制 + 同主机信任域):接受「同主机=同 ns 全插件共享信任域」;agent 已**忽略 worker 传入 ns 恒用自己的**,越 ns 面已堵(见 §2.4)。
> - **M6**(镜像 allowlist 默认空=放行全部):**不**要求生产 allowlist 必填(那是部署摩擦);控制面入口本就 admin token 守着。
> - **M7**(发现的 dir/image 驱动写寻址扩大越权面):这是 §3.3「推翻 v3 发现权威」的代价;沿用 `validate_managed_dir`(realpath 须在 `MANAGED_PROJECTS_ROOT` 下)兜底即可,不新增强约束。
> - **M9**(caller header 可伪造):已在 §2.5 改为「caller 只取 agent 可验证的源 IP 反查」——这条属审计正确性,顺带收口。
>
> **共同底线**:不为这些给部署加 sha256 校验 / 强绑 127.0.0.1 / allowlist 必填 等摩擦,均不作硬门、不阻塞上线;`validate_managed_dir` 等**既有**兜底保留。若将来该端口需跨网段暴露,再回头逐条提级。评审全文见 `archive/review-ultracode-2026-06-21.zh-CN.md`。

## 6. 健壮性(失败与启动)

- **worker 启动依赖本机 agent**:
  - agent 未起 / 无缓存且回源失败 → worker **回退上次已装插件**(本地 last-good),**绝不白屏**,记 warning。
  - 启动顺序:agent 常驻;worker `restart: always`,重启时 agent 已在。同 compose 可 `depends_on`。
- **agent 回源失败**(平台挂/网络):用缓存(可能旧版)兜底,标 `stale`;平台恢复后下次刷新。
- **缓存损坏/校验失败** → 丢弃重拉。
- **幂等/并发**:同 attachmentId 走缓存;并发同包 per-key 锁。
- **区分「agent 不可达」与「agent 说你没有插件」(评审 M4)** —— 现 `sync-plugins.js` 只有 fetchJson 抛错才走 last-good;**agent 可达但返回空数组**会命中「列表为空,跳过同步」直接 return,全新容器零业务插件仍可能白屏且**无 warning**;**单插件 download 失败**被隔离计数后脚本仍 `exit 0`,带缺失插件照常跑。P2 验收**必须补两条**:(a) agent 可达但返回空 → worker **保留既有 `storage/plugins` 不清空**且记 warning(不是 last-good 路径);(b) 清单非空但某插件 download 失败 → 固化「隔离继续 vs 拦截启动」的产品决策(建议**首装必需插件缺失时让 init 失败**)。两种语义不可混为一谈:「不可达」用 last-good,「说你没有」可能是误配。
- **agent 挂期间该主机不可运维(评审 M8)**:agent 是该主机唯一 docker 执行点,挂时 §4.2 启动/停止/更新**全不可用**;而 worker last-good 只覆盖「**启动时**拉插件」,**不覆盖**「agent 挂期间需重启/重部署」。§11 风险表须列此项并给运维降级预案(人工上机 / 等 agent 恢复)。

## 7. 可观测

- **agent `/health` 扩展**:缓存命中率 / 回源次数 / 本机服务清单 / pull-token 有效性 / 上次上报时间 / last-good 是否在用。
- **platform**:`fetch_records`(**回源粒度**审计,caller 取 agent 反查值;per-container 看 agent 本地日志,见 §2.5)、DiscoveredNode 台账(在线/心跳/`stale`)、发布运行记录(§8)、发布→投放关联视图。
- **hub**:命令审计(已有)。
- **实例实时日志**(§4.3):实例页一键看 `docker logs -f` 实时 tail(浏览器 SSE ⇄ hub ⇄ agent 流式),仅实时不落库;离线/stale 实例不可用。

## 8. 数据模型变更

- **platform**:
  - 新增 `DiscoveredNode`(agentId, composeProject, containerName, dir, image, host, nacosService, running, healthy, heartbeatAt, **status**)—— 自动 upsert;心跳超 TTL **标 `stale`/`unknown` 不删行**(评审 M8,见 §3.2),`status` 区分 active/stale/unknown。
  - `Service` 退化为纯逻辑层(分发单位),**不再扛 dir/镜像寻址**(dir/镜像权威在 DiscoveredNode,见 §3.3 推翻 v3)。
  - `fetch_record` = **回源粒度**(ns/service);`caller` 取 **agent 反查的源容器/工程**(非 worker 自报),per-container 完整审计在 agent 本地日志(评审 M2/M9,见 §2.5)。
  - distribution 响应可选加 `sha256`(缓存校验,向后兼容;按 §5「安全取舍」不强制、不阻塞)。
  - **发布运行记录(新,评审 G5)**:`Rollout`(id, nacosService, 目标插件版本/目标镜像, 实例序列, 各实例状态, 失败点, mode=freeze|rollback, createdAt)→ 支撑「失败即停」后的断点续投 / 回滚,与原型进度视图对齐。
  - 「已纳管」= Service 表里有行(无需额外 flag);「已发现未纳管」收件箱 = 发现的 nacosService ∉ Service 表;对账 = `Service ⋈ DiscoveredNode`(by nacosServiceName)的 join 视图(不落表,实时算)。
  - **插件维度**:`service_plugins`(service↔plugin 绑定)+ 每绑定的 active 版本(`service_plugin_version`)→ 支撑「绑定/解绑/改版本」。
  - **镜像维度(新)**:`ServiceImage`(service_id, image, isCurrent, createdAt)历史表(或 Service.currentImage + 历史行)→ 支撑镜像「当前 / 历史 / 回滚」;回滚 = 置某历史 tag 为 current + pull-redeploy。镜像本身仍是节点级(发现上报真实运行 tag),这里存的是「该服务**应用**哪个镜像」的意图 + 历史。
- **hub**:加「发现上报」接收 + 落库 + 心跳清理。(实时日志 SSE `app/routers/logs.py` **已有**,§4.3,无需新增。)
- **agent**:新增 `plugin_cache` 模块 + worker-facing HTTP 路由 + 周期发现上报 + 配置项 `PLATFORM_URL` / `PULL_TOKEN` / `PLUGIN_CACHE_DIR` / `PLUGIN_SERVE_PORT`。(实时日志流式 `core/log_sessions.py` **已有**,§4.3,无需新增。)
- **platform**:实例页**日志查看器 UI** 接 hub 既有 SSE(§4.3);其余实例/服务/纳管/镜像页。

## 9. worker 侧(`docker/nocobase/sync-plugins.js`)—— 基本是改配置,不重写

读了现有客户端(cnp `docker/nocobase/sync-plugins.js`,容器启动 init 脚本,NocoBase 起来前跑),它**已经足够灵活**,方案 A 下 worker 侧几乎零代码改动:

**现有客户端已具备的(无需新建)**:
- `adminUrl` + **`apiPath` 均可配**(`CONFIG.apiPath` / `ORCHISKY_ADMIN_API_PATH`)→ 指向本机 agent 只是改配置(老路径 `/api/t_service_hub:queryPlugin` 本就是配出来的)。
- **token 可选**:不配 `adminToken` → curl/请求**不带 Authorization** → 天然适配 tokenless 的本机 agent。
- 下载用 manifest 返回的 `url`(curl,`rejectUnauthorized:false` 时 `-k`)→ agent 把 `url` 写成指向自己即可。
- 版本比对**优先用响应的 `version` 字段**(`plugin.version || extractVersionFromUrl`)→ `/download/{attachmentId}` 这种 URL 无版本名也 OK。
- **last-good 兜底已存在**:`fetchJson` 失败(含 agent 连不上)→ catch → "使用本地已有版本";单插件下载失败隔离、不影响其他。
- 安装链(curl→tar→定位 package/→copy 到 `storage/plugins/<name>`→node_modules 软链→对首装包 `pm enable`)成熟,**不动**。

**方案 A 下 worker 侧改动 = 改 `sync-plugins.config.json`**:
```json
{
  "adminUrl": "http://<本机 agent host>:<PLUGIN_SERVE_PORT>",
  "apiPath":  "/plugins",
  "namespace": "<ns>",          // 必填:现 mode2 以 namespace 非空为触发前提(见下 L3);agent 忽略其值、恒用自己的本 ns
  "service":  "wms-test-admin"
  // 去掉 adminToken —— 本机 agent 无需 token
}
```

**agent 侧的硬约束**(对齐现有客户端,保证 worker 仅改配置):
- `GET /plugins?service=` → **纯数组** `[{pluginName, version, url}]`,字段字面不改名;`url` 指向 agent 自己的 `/download/...`。
- 客户端会跟随 301/302/307/308 重定向 → agent 也可只返回平台原 url 让 worker 直连(过渡期"透传模式"),或返回自己 url 走缓存("缓存模式")。
- ⚠️ **namespace 不是"可选可不传"(评审 L3 + G2 收口)**:现 sync-plugins **mode2 进入条件含 `&& NAMESPACE`**(`else if (ADMIN_URL && NAMESPACE && SERVICE)`),namespace 为空会落到「未配置任何来源」**整段跳过同步**(连 last-good 都不走),且 URL 恒拼 namespace——所以"不传也能跑"为假。二选一:**(推荐·零代码改)** 配置里**保留 namespace 字段**(agent 忽略其值,worker 无 ns 逻辑);或要 worker 彻底不带 ns,则**必须改 mode2 gate**(去 `&& NAMESPACE` + 按需省略 URL 拼接)——属**必改、非可选微调**。

> 📌 **跨仓交付 + 契约固化(评审 G1)**:`sync-plugins.js` 在 **cnp 仓库**,本设计与 service-agent/platform 在 **services-monorepo**(另一仓)。§9 所有「现有客户端已具备」都是对 cnp 那份**当前实现**的断言,本仓无法验证、也不随版本一起交付——cnp 脚本一旦与假设漂移,P2 真机静默失败,而平台侧 e2e 测不到 cnp 镜像里的脚本。**P0 把 worker↔agent `/plugins` 契约固化成 contract test**(重定向跟随、version 优先、tokenless、last-good、空清单不清空 等关键行为写成断言),用最小 fake-worker 在本仓 agent e2e 跑;文档标注 cnp 侧脚本 **pin 的 commit + 负责人**,改 agent 契约时**两仓同 PR**。

## 10. 落地分期(完整路线)

| 阶段 | 内容 | 验收(rolltest 床 e2e) |
| --- | --- | --- |
| **P0** | 契约定稿(worker↔agent **零 ns**、发现上报、数据模型、**失败即停回滚语义** G5)+ **worker↔agent contract test**(G1)+ 本设计冻结 | 评审通过;contract test 绿 |
| **P1** | agent:`plugin_cache` + worker-facing `/plugins`、`/download`(回源平台,持 token);配置项落地 | worker 经 agent 拉到插件(缓存命中/回源各一次) |
| **P2** | worker:改 `sync-plugins.config.json`(adminUrl→本机 agent、apiPath→`/plugins`、去 token、**保留 namespace 字段**);若要彻底去 ns 则改 mode2 gate(L3,必改非可选) | 真 worker 从本机 agent 装插件成功;agent 挂走 last-good 不白屏;**agent 返回空清单 → 不清空既有插件 + 记 warning**(M4) |
| **P3** | 自动发现:agent 周期 nacos+**docker(含 stopped,新采集器)**上报 → hub 落库 + 心跳(标 stale 不删)→ platform 节点表 + UI;Service auto-upsert;**实例实时日志 UI**(§4.3,agent+hub SSE **已有**,仅接 platform 实例页查看器) | UI 自动出现 admin/2admin 两节点(含已停),dir 自动填,无需手配;实例页接 hub SSE 看实时日志 |
| **P4** | 投放联动(**依赖 §4.1 协调器、同批**):platform 发布 → hub **跨 agent 顺序投放**(restart / pull-redeploy 共用协调器 + 集群健康门 + 失败即停回滚)→ worker 重拉;agent 预热 | 改 active → 跨机顺序滚 → 都跑新版零中断;**注入失败 → 失败即停 + 收敛**(G5) |
| **P5** | 加固:fetch_record caller 维度、灰度发布、缓存 sha256 校验、可观测面板、**迁移老节点**(root-JWT→pull-token、adminUrl→agent) | 老节点平滑迁入;审计/监控齐全 |

## 11. 风险与权衡

- **agent 进 worker 启动关键路径** → last-good 缓存 + 启动顺序兜住;agent 本身 `restart: always` 常驻。
- **agent 挂 = 该主机不可运维(评审 M8)** → agent 是该主机唯一 docker 执行点,挂时启动/停止/更新全不可用;DiscoveredNode 标 `stale` **保留**(不蒸发,供恢复后定位);降级预案:人工上机 / 等 agent 恢复。
- **同主机 tokenless 信任 + 一批安全项** → 按 §5「安全取舍」整体接受(内网信任域;H1/H2/M5/M6/M7/M9 有意降级,不加部署摩擦)。
- **包不过 WS** → 已定:列表可走任意通道,包一律 HTTP。
- **nacos 只见在跑的** → docker(含 stopped)为主、nacos 补(该采集器 P3 待建)。
- **all-in-one vs 分离**:本方案 agent 直连 platform(A),两种部署都成立;all-in-one 下 platform 即 console:80 的 `/api/distribution/*`。

## 12. 现状对照(已有 / 待建)

| 能力 | 现状 |
| --- | --- |
| 平台分发端点 `/api/distribution/{plugins,download}` + pull-token + IDOR | ✅ 已有(`distribution.py`) |
| 插件/版本/服务-插件/发布/拉取审计 数据模型 | ✅ 已有 |
| agent 入站 HTTP server | ✅ 已有骨架(`health_server.py`,可扩展) |
| agent nacos 发现 + 本机实例匹配 + docker **running** 调用 | ✅ 已有(`nacos_client`/`instance_match`/`docker_cli`),但 `docker_cli` 仅 `docker ps -q` 列 **running** |
| **agent 采集「含 stopped 容器/工程」**(发现链基石,§3.1 行109) | ❌ 待建(P3;需新增 `docker ps -a`/`compose ls -a`,现**零实现**——"已停也可管"当前不成立) |
| 控制链 node-control · **单机多容器**滚动 / dispatch | ✅ 已有 |
| **跨 agent(多节点)顺序滚动协调**(§4.1) | ❌ 待建(P4;现 hub `_run_rolling` 见 unmatched 即整体 abort,跨机滚不动) |
| **agent 插件缓存 + worker-facing /plugins,/download** | ❌ 待建(P1) |
| worker 客户端 `sync-plugins.js` | ⚠️ 在 **cnp**(跨仓,G1),apiPath/url/token 可配 + last-good 已有 → **P2 主要改配置**;但 namespace 非可省(L3)、空清单/单失败会静默少装(M4)需补验收,且需 contract test 固化跨仓契约 |
| **agent 拓扑自动发现上报 + hub 落库心跳(标 stale 不删)+ platform 节点表** | ❌ 待建(P3) |
| **发布→滚动投放联动**(publish/rollback 现只改 DB active,不触发控制链 M1)**+ 预热** | ❌ 待建(P4,依赖跨 agent 协调器) |
| 服务「插件配置」管理面(绑定/解绑/改版本) | ⚠️ 后端模型多在(service_plugins/releases),缺二级页 UI |
| 服务「镜像配置」管理面(当前/历史/回滚 → pull-redeploy) | ❌ 待建(原型已设计;pull-redeploy 能力 agent 侧已有,缺平台镜像台账 + 回滚 UI) |
| **实例实时日志**(§4.3:agent `compose logs -f` 流式 → hub SSE → UI) | ⚠️ **大部分已有**:agent `core/log_sessions.py` + hub `app/routers/logs.py`(SSE+fan-out+鉴权)+ e2e 均已落地(node-control);**仅 platform 实例页查看器 UI 待建**(P3,小) |
