# 插件分发改造 + console 合并 · 总开发计划

> **唯一主计划**(已并入原 `console-merge-plan`)。依据:`plugin-distribution-redesign.zh-CN.md`(设计冻结基线)+ `archive/review-ultracode-2026-06-21.zh-CN.md` + `review-ultracode-2026-06-22.zh-CN.md`(两轮评审收口)。
> 把设计 + 「hub/platform 合并为 service-console」翻成**可分配任务**,每条标 仓库 / 模块 / 前置 / 验收。
> **仓库**:`services-monorepo`(合并后只剩 **service-console** + **service-agent**)与 `cnp`(`docker/nocobase/sync-plugins.js`)。
> **决策(2026-06-22)**:① 彻底合并 hub+platform → **service-console**(一服务一 DB;DB 压成单一初始迁移);落点重命名 service-console;agent `plugin_cache` 保留。② 失败收敛 **mode = freeze**(失败即停 + 标记 + 人工决定重试/回滚,不自动回滚)。③ 跨机服务发布 = **按 `nacos_service_name` 事务扇出置活**(单一真相)。
> **基调**:agent 内网、不暴露外网;**部署简单 = 最高优先级**;安全类(H1/H2/M5/M6/M7/M9/L4)有意降级、不加部署摩擦(设计 §5)。

## 0. 阶段与依赖

```
P0 契约+冻结 ─┬─→ M  hub+platform 合并为 service-console (S1–S8) ──→ P3/P4/P5(console 控制面特性,进程内)
              └─→ A  agent 插件分发(P1 缓存 / worker-facing / 回源 + P2 worker)   [可与 M 并行]
P4 的 publish→自动滚动 依赖 §4.1 协调器(同批,不可早于它)
```

关键顺序:
- **P0 阻塞一切**;**P0-3(freeze 收敛语义)/ P0-4(跨机扇出 + 字段)必须早于 P4**。
- **M(合并)尽早**;**P3/P4/P5 控制面特性排在 M 之后**(免得在 hub/platform 上建完又重并)。
- **A(agent 侧)可与 M 并行**;P2 依赖 P1。
- **P4 自动触发依赖 §4.1 协调器**(评审 M1/L1),二者同批。

**进度**:P0 设计冻结基线已出 · **P1-1 ✓**(167cc65)· **P1-4 ✓** · 计划已并 console-merge(1dbdfae)· 已过 ultracode 二轮评审(2026-06-22)收口 · **M 合并 S1–S8 全部完成 ✅**(96c8c71→2d0dd71;hub+platform→单一 service-console,单库 12 表,进程内直调,373 测试绿,顶层只剩 service-agent+service-console)· ultracode 遗留清理 ✅(7434e73)+ fixture 收口 ✅(99a245f)· **P1-2/P1-3 ✓**(agent worker-facing server + 回源;213 agent 测试绿,覆盖率 97.77%)。**下一步:P2(worker `sync-plugins.js` 配置 + mode2 gate + M4 验收)、P3+(发现/实例页/投放,均落 service-console)**。

---

## P0 — 契约 + 设计冻结(阻塞后续)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P0-1 | **worker↔agent `/plugins` 契约定稿**:worker **零 ns**(agent 忽略传入 ns 恒用本 ns)、字段 `pluginName/version/url` 字面不变、`url` 指向 agent 自己、tokenless;关键行为=重定向跟随、version 优先、last-good、**空清单不清空**(评审 G2/M4/M5) | 契约文档 + cnp(pin sync-plugins commit) | — | 契约文档冻结 |
| P0-2 | **contract test**:P0-1 关键行为写成断言,最小 fake-worker 在 agent e2e 跑;cnp 脚本 **pin commit + 负责人**,改契约两仓同 PR(评审 G1) | services-monorepo(agent e2e)+ cnp | P0-1 | contract test 绿;cnp 脚本 commit 已 pin |
| P0-3 | **失败收敛语义定稿 = freeze**(决策已定):滚动中某实例失败 → **失败即停 + 标记「停在第 N 个」+ 人工重试/回滚**(不自动回滚);`Rollout` 记录字段(目标版本、实例序列、各实例状态、失败点、mode=freeze)。**注**:freeze 下无需 per-实例「上一 deployed 版本」字段(自动回滚才需);若原型保留「手动回滚」按钮则其实现走人工重投上一版,非自动(评审 G5/M8) | service-console(设计/数据模型) | — | 语义 + `Rollout` 字段冻结;明确"手动回滚"取上一版来源 |
| P0-4 | **字段语义 + 跨机发布模型冻结**:`service_code`(分发)≠ `nacos_service_name`(发现/滚动)两列;dir/镜像权威 = `DiscoveredNode`(**推翻 node-control v3**,评审 H4/H5);`agentKey` 生命周期(provision/存储 hash/校验/rotate,评审 G6)。**跨机服务发布(决策已定)= 按 `nacos_service_name` 找出所有 ns-Service 行、事务性同步置活**(定义部分失败语义:任一失败整体回滚 active),对账按 `nacos_service_name` 检 N 行 active 不一致告警;**消除设计 §4「单一真相」与 §4.1「跨多 ns」措辞冲突**(评审 H-5) | service-console(`db_models.py`/`releases.py` 设计) | — | 数据模型 + 跨机扇出契约冻结 |
| P0-5 | **安全取舍确认入冻结**:H1/H2/M5/M6/M7/M9/L4 内网降级、不加部署摩擦;保留 `validate_managed_dir` 兜底 | service-console(设计 §5) | — | 写入冻结基线 |

---

## M — hub + platform 合并为 service-console(S1–S8)

> 终态:单 FastAPI = platform 路由 + SPA + hub `/ws/agent` + hub 路由(logs/rolling/nodes)+ hub store;一个 DB + 一套 Alembic;`hub_client.py` 改造为进程内 async 适配器(保留 7 函数名 + 契约,函数体 await 调 app/hub handler,删 `SERVICE_HUB_URL`/`HUB_ADMIN_TOKEN` + httpx 依赖);all-in-one 单进程;agent 不动。

| 步 | 内容 | 测试门 |
| --- | --- | --- |
| **S1 ✓** | 落点重命名 `git mv service-platform service-console`;修目录名引用(app/ci.yml);镜像/deploy 类留 S7 | console 测试 263 passed(已过) |
| **S2 ✓** | 并入 hub 代码 + WS 端点。子项:① hub `app`(store/routers/ws/models/config/api_support/force_guard)并入 console;② `main.py` include hub 路由 + 挂 `/ws/agent`;③ **合并模块级单例**(`database`/`hub_state`/`logger` 落点唯一,沿用 console「单例唯一落点 + 函数内延迟 import」约定,过渡期允许两 `Database` 实例并存);④ **合并两个 lifespan,务必保留 hub 的 `hub_state.initialize` + `interrupt_running_rolling`**(否则重启后中断的滚动永不被标 interrupted);⑤ 确认 WS 握手能过中间件(BaseHTTPMiddleware 不处理 websocket scope,需 e2e 验)(评审 M-9) | **WS agent 能连入 + 启动恢复中断滚动**;hub 路由可访问;import 修通 |
| **S3 ✓** | 合 config:hub settings 并入 console 一份(`SERVICE_HUB_URL`/`HUB_ADMIN_TOKEN` 等内部项删) | config 测试绿 |
| **S4 ✓** | 合 DB + 迁移(**高风险,评审 H-1**)。子项:(a) **合并两个 `db.py` 的 `_managed_tables` 守卫为 12 张全集**(console 8 + hub 4)并保留「部分初始化→RuntimeError」语义,加**旧库升级路径迁移测试**;(b) 统一 `db.py`(一个 engine/Base,统一 `created_at/updated_at` 时区);(c) **删两套 `migrations/`,单一 `0001_initial` 必须 `alembic autogenerate` against 合并后 `Base.metadata`——禁手工拼接两个旧 0001**(否则静默丢 hub 增量迁移的 5 个 delta 列:`commands.mode/retry_count/original_request_id`、`agents.agent_key_hash/key_issued_at`,`store.py` 运行时实读) | **老库 + 新库双路径建表均绿 + 列集断言**(新库列集 == console 0001 ∪ hub 0001+0002+0003+0005,尤校上述 5 列) |
| **S5 ✓** | 进程内直调,删 hub_client(**高风险,评审 H-2**)。子项:① 列 **7 个 `hub_client` 函数**(provision/list_agents/list_instances/dispatch_command/rolling_restart/list_commands/rotate_agent_key)→ 对应 `hub_state`/store 内部方法**映射表**,标 **sync→async 边界**;② 改 `nodes.py` **5 处调用点**(`:83/143/267/313/337/373`)为进程内直调,**必须保留 per-agent 短超时 `asyncio.wait_for` + `gather(return_exceptions)` 隔离**(否则单 agent WS 卡死吊死整页);③ `dispatch/rolling` 的 `requested_by/request_source` 改进程内取值来源(不再有 admin-token 服务端派生);④ 重写 `test_nodes`(~34 处 monkeypatch 桩)/`test_hub_client`/namespaces 测试桩;⑤ `hub_client.py` 改造为进程内 async 适配器(保留 7 函数名 + 契约,函数体 await 调 app/hub handler,删 `SERVICE_HUB_URL`/`HUB_ADMIN_TOKEN` + httpx 依赖)+ 相关 config | console 全量绿 + **「单 agent 卡死整页仍响应」degraded 回归** + caller 身份正确 |
| **S6 ✓** | 合测试:hub tests 并入 console;修 import;原 platform+hub 用例(+调整)全绿 | 全量绿 |
| **S7 ✓** | 镜像/CI/nginx:all-in-one Dockerfile/supervisord 改单进程;nginx 单上游;`docker-publish.yml` 单 `service-console` 镜像、删 hub 镜像 job;删除旧三容器部署栈 `service-console/deploy/`,统一由 `deploy/all-in-one` 承担;**补 `PLATFORM_URL` 进 agent 部署模板并注明须与 `WS_URL` 同指一台 console**(评审 M-10) | 镜像构建通过;57 床冒烟;agent 仅配一处 console 地址即可 WS 连接 + 回源 |
| **S8 ✓** | 清理:删 `service-hub` 旧目录;文档/README 指向 service-console;全量 + e2e(ws/logs/rolling) | 全量 + e2e 绿 |

**M 风险门**:S4/S5 最险,各完成后跑 console 全量;S2 WS 整合后跑 `validate_logs_stream_e2e`/`validate_phase1_e2e`;旧目录到 S8 才删。

---

## P1 — agent 插件缓存 + worker-facing 端点(主战场;与 M 并行)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| **P1-1 ✓** | **agent 插件缓存模块**:内容寻址(键=`attachmentId`)、容量上限 + LRU、`PLUGIN_CACHE_DIR`、并发同包 per-key 锁 | service-agent `services/plugin_cache.py` | P0 | **已完成**(167cc65,7 例 + 全量 179 passed) |
| **P1-2 ✓** | **worker-facing HTTP**:`GET /plugins?service=` + `GET /download/{attachmentId}`;**独立 server 绑 `PLUGIN_SERVE_HOST` 默认 `127.0.0.1`**(不复用 `HEALTH_HOST` 的 0.0.0.0)、`PLUGIN_SERVE_PORT`;**忽略 worker 传入 ns、恒用本 ns** | service-agent `core/plugin_server.py` | P1-1 | **已完成**(`maybe_start_plugin_server` 仅配齐才起;url 改写 echo Host;未配→503;213 测试绿)。**rolltest 床 curl 验收待联调** |
| **P1-3 ✓** | **agent→console 回源**:持 pull-token 调 console `/api/distribution/{plugins,download}`,清单 `url` 改写指向自己;回源喂 `plugin_cache.get_or_fetch` | service-agent `services/plugin_distribution.py`(复用 console `distribution.py`) | P1-1 | **已完成**(`fetch_manifest`/`attachment_id_from_url`/`download_to`;namespace 恒用本 ns;`http_client.download` 流式禁跳转;未命中→回源一次→落缓存→喂 worker) |
| **P1-4 ✓** | 配置项:`PLATFORM_URL`/`PULL_TOKEN`/`PLUGIN_NAMESPACE`/`PLUGIN_CACHE_DIR`/`PLUGIN_CACHE_MAX_BYTES`/`PLUGIN_SERVE_HOST`/`PLUGIN_SERVE_PORT` | service-agent `config.py` | — | **已完成**(随 P1-1) |

**P1 验收(rolltest 床)**:worker 经本机 agent 拉到插件,缓存命中 / 回源各一次。

---

## P2 — worker 改配置(cnp)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P2-1 | 改 worker 的 `sync-plugins.config.json`(**部署侧运行时文件,不在 cnp 仓**,评审 L-8):`adminUrl`→本机 agent、`apiPath`=`/plugins`、去 `adminToken`、**保留 `namespace` 字段**(agent 忽略其值)。P0-2 pin 的是 cnp 仓 `sync-plugins.js` + `sync-plugins.config.json.example` | cnp(`.js` + `.example`)+ 部署模板 | P1 | 真 worker 从本机 agent 装插件成功 |
| P2-2 | **(条件)** worker 彻底无 ns:改 `sync-plugins.js` **mode2 gate**(去 `&& NAMESPACE` + 按需省略 URL)——**必改非可选**(评审 L3) | cnp(`docker/nocobase/sync-plugins.js`) | P1 | 不带 ns 仍能同步 |
| P2-3 | **验收补 M4**:(a) agent 可达返回空 → 不清空既有 `storage/plugins` + warning;(b) 单插件 download 失败 → 固化「隔离继续 vs 拦截启动」(建议首装必需插件缺失则 init 失败) | cnp + 验收用例 | P1 | 两类部分失败行为符合预期 |

**P2 验收**:worker 装插件成功;agent 挂走 last-good 不白屏;空清单不误清。

---

## P3 — 自动发现 + 实例页 + 服务管理面 + 实例日志(在 service-console,M 之后)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| **P3-1 ✓** | **docker「含 stopped」采集**(评审 H3):`docker_cli.list_all_containers`(`docker ps -aq`+inspect)+ `discovery.collect_local_containers`(compose label 过滤 + managed_root 限定)组装 `[{containerId, containerName, composeProject, composeService, dir, image, running}]`;nacosService/healthy/host 由 P3-3 结合 nacos 补。**已停容器**(exited 未删)即覆盖;`compose ls -a`(容器已 down 删除的零容器工程)如需再补 | service-agent `docker_cli.py` + 新 `discovery.py` | P0 | **已完成**(227 测试绿,覆盖率 97.86%) |
| **P3-2 ✓(agent 侧)** | **instance→agent 映射 + 落位防错**(评审 M3):`instance_match.matching_containers`(返回全部候选,不取第一个)+ `discovery.enrich_with_nacos`(按 ip:port 反查给容器补 nacosService/healthy)。**双向冲突检测告警**:一实例多容器(端口主键失败落 IP 兜底撞)、一容器多实例(歧义则该容器 nacosService/healthy 置 None 不猜)。匹配优先级:宿主发布端口→bridge IP。**console 侧落库唯一性校验并入 P3-4**;nacos 注册端口语义(宿主 vs 容器内)属部署侧钉清 | service-agent `instance_match.py`/`discovery.py` + (console P3-4) | P3-1 | **agent 侧已完成**(237 测试绿,97.92%);admin/2admin 不张冠李戴 |
| **P3-3 ✓** | **agent 周期发现上报线程**(评审 H-6):`core/discovery_reporter.py` 每 `DISCOVERY_INTERVAL`(默认 30s,<=0 禁用)调 docker(含 stopped)+ `nacos_client.list_all_instances`(新:全服务全实例含不健康)+ discovery 组装/落位,经当前 WS 主动上报;线程在 `_on_open` 随心跳启动、随 `ws.keep_running` 退出;单轮异常只记日志不挂线程;nacos 未配/失败则只报 docker 侧。**上报契约**(对齐 P3-4):`{type:"discovery-report", agentId, nodes:[{containerId,containerName,composeProject,composeService,dir,image,running,nacosService,healthy}], warnings, ts}` | service-agent `core/discovery_reporter.py` + `ws_client`/`nacos_client` | P3-1, P3-2 | **已完成**(249 测试绿,98.04%);agent 连上后周期上报 |
| P3-4 | **console 接收发现上报 + 落库 + 心跳**:**标 `stale`/`unknown` 不删行**(评审 M8),显式确认下线才删 | service-console(store + 新消息类型) | M, P3-3 | agent 失联节点保留可定位 |
| P3-5 | **DiscoveredNode 表 + 实例页 UI**:全自动 upsert;dir/镜像权威在此(P0-4) | service-console(`db_models.py` + SPA 实例页) | P3-4 | UI 自动出现节点,dir 自动填,无需手配 |
| P3-6 | **node-operations 寻址迁到 DiscoveredNode**(评审 H-3,撞 P0-4 冻结契约):重写 `nodes.py` 的 `_resolve_service`/`_derive_health_base_url`/`_compose_default_project`/`dispatch_node_action`,寻址源 Service 行 → DiscoveredNode 行(按 `agentId+nacosService` 或 `composeProject` 定位 dir/image/containerId);明确 `Service.dir/default_image` 退化为 null 后取值/回退 | service-console(`nodes.py`) | P3-5 | Service.dir 为空但 DiscoveredNode 有 dir 时实例运维仍可下发 |
| P3-7 | **纳管模型**:发现 `nacosService` ∉ Service → 「已发现未纳管」收件箱;一键纳管(预填 ns+nacosName,**两列 service_code/nacos_service_name**)+ 人工配插件;对账三态(running-but-unmanaged / managed-but-down / version-drift),**跨 agent 同名服务按 `nacos_service_name` 聚合**(评审 H-5/M-7) | service-console | P3-5 | 收件箱 + 对账视图可用;跨机服务按 nacos 名聚合 |
| P3-8 | **服务「插件配置」二级页 UI**(评审 H-4①;后端模型 `service_plugins`/`service_plugin_version` 多已有,缺 UI):绑定/解绑、改版本(选版本)| service-console(SPA) | P3-5 | 配置二级页可用,写回后服务详情同步 |
| P3-9 | **实例实时日志 UI(§4.3)—— 后端已有,合并后同进程**:agent `core/log_sessions.py` + console `app/routers/logs.py`(SSE,fan-out,e2e)**均已落地**;本任务只做 console 实例页日志查看器 UI:SPA 直连 console SSE,`dir` 取自 DiscoveredNode、`agentId` 取自所属 agent;**复用既有 `sessionId/dir/logs_*` 协议,勿另造** | service-console(SPA) | M, P3-5 | 实例页看实时日志;关闭即断;离线实例不可用 |
| P3-10 | **console SPA 顶栏命名空间切换器**(评审 L-6;原型已实现,现 SPA `AppShell` 无):切 ns → 实例/服务/获取记录及统计卡按当前 ns 过滤;**跨机服务(跨 ns)需提供不被 ns 切散的 by-nacosService 聚合视图**(评审 L-7) | service-console(SPA `AppShell`) | P3-5 | 切 ns 过滤各页;跨机服务有聚合入口 |

**P3 验收**:UI 自动出现 admin/2admin 等节点(含已停);实例页接 SSE 看实时日志;实例运维按 DiscoveredNode 寻址。

---

## P4 — 投放联动(service-console 进程内;依赖 §4.1 协调器)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P4-0 | **跨机聚合查询**(评审 M-11/L-9):基于 DiscoveredNode 实现「按 `nacos_service_name` 聚合得 `{agent:[实例]}`」,作为协调器与三态对账共同输入;为「实例→agent 映射」定轻量契约(`nacosService/host/agentId/composeProject/containerName`) | service-console | P3-3, P3-4 | 同 nacosService 跨 2 agent 能正确分组 |
| P4-1 | **跨 agent 顺序协调器**(评审 M10/L1):`_run_rolling`「有 unmatched 就 abort」改「过滤 unmatched、只滚本机 matched」(agent 无需改);加 for-each-agent **串行** + **集群级健康门**(滚 A 期间持续重探 B 健康、排除正在重启实例) | service-console(`rolling.py`) | M, P4-0 | 跨机顺序滚、零中断、失败即停(freeze) |
| P4-2 | **publish/rollback 触发控制链**(评审 M1,现只改 DB active):active 变更 → 触发滚动(**进程内直调,不经 hub_client**);**跨机服务按 P0-4 事务扇出置活后一并触发**;restart / pull-redeploy 共用 P4-1 协调器(评审 M11) | service-console(`releases.py` + `rolling.py`) | M, P4-1 | 改 active → 自动滚 → 实例跑新版;跨机一次 publish 扇出 N ns |
| P4-3 | **失败即停(freeze)+ 收敛记录**:`Rollout` 运行记录(P0-3)落库;失败 → 标记 + 停后续 + **冻结半迁移态等人工**(不自动回滚);提供人工重试/手动回滚(走重投上一版)入口 | service-console | P4-1 | 注入失败 → 失败即停 + 状态可见可人工处置 |
| P4-4 | **服务「镜像配置」二级页 + ServiceImage 台账**(评审 H-4②):`db_models` 加 `ServiceImage`(service_id, image, isCurrent, createdAt)历史表 + 迁移 + store/路由;UI 当前/历史/**回滚→pull-redeploy**;`nodes.py` redeploy 改读 ServiceImage 当前行(与 P3-6 对齐) | service-console(`db_models`+迁移+路由+SPA) | M, P4-1 | 镜像配置页可用;回滚走 pull-redeploy |
| P4-5 | **统一 desired-state 发布 UI**(评审 H-4③④):发布弹窗 = 暂存意图 → svcDiff → **按 diff 自动选 restart/pull-redeploy** + 变更摘要 + 逐实例进度;**镜像漂移纳入实例页 + 服务对账(镜像)**;**漂移强制重投(服务级 reapply)/同步漂移实例(单实例),不改意图**与「diff 发布」并列为两条路径(评审 L-5) | service-console(SPA + P4-1/P4-4 后端) | P4-1, P4-4 | 发布弹窗按 diff 选机制;镜像漂移可见可修 |
| P4-6 | agent **预热**:console 通知「新版本将至」→ agent 提前回源缓存 | service-console + service-agent | P1 | 滚动时 worker 拉取零等待 |

**P4 验收**:改 active → 跨机顺序滚都跑新版零中断;注入失败 → 失败即停(freeze)+ 可人工处置;镜像/插件两管理面 + desired-state 发布可交付。

---

## P5 — 加固 / 迁移(收尾,可并行)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P5-1 | **审计口径 = 回源粒度**(评审 M2/M9/H-7,决策已定):console `fetch_record` = **回源粒度**(ns/service + 回源版本/时间),不承诺 per-container;caller 取 agent 反查(源 IP→容器),不信 worker 自报 header;**原型「获取记录」已改回源粒度口径**;per-container 审计若需则走 agent 本地日志(可选,另起任务) | service-console + service-agent | P1 | 审计口径=回源粒度、不可伪造 |
| P5-2 | 灰度发布(只滚部分实例 / 按 DiscoveredNode 子集) | service-console | P4 | 可灰度 |
| P5-3 | 缓存 sha256 校验(**可选**,按 §5 取舍不阻塞、不强制) | service-console + agent | P1 | 启用后校验生效 |
| P5-4 | 可观测面板(agent /health 扩展、`Rollout` 发布运行记录、发布→投放关联视图) | service-console + agent | P3/P4 | 面板齐全 |
| P5-5 | **迁移老节点**:root-JWT→pull-token、adminUrl→本机 agent;生成/下发新 `sync-plugins.config.json` 到各 worker | cnp + 部署 | P1/P2 | 老节点平滑迁入 |

---

## 文档/原型一致性收口(评审 C 组,与施工并行)

- ✅ **设计文档补合并决策注 + 收口 platform/hub 措辞 → service-console**(评审 M-5):顶部已加「覆盖性声明」;§1 组件表(platform+hub 并为 service-console)、§5 安全表(删除已不存在的 platform→hub admin-token 链)已收口;§8/§12 的「hub」作为 console 进程内模块名(`app/hub/`)保留,由覆盖性声明统一口径;§4「单一真相」/§4.1「跨多 ns」由 P0-4 扇出模型消解。
- **原型演示态自洽**(评审 M-1~M-4/M-6/M-7/L-1/L-2/L-3/H-7):managed 表数据驱动 + 一键纳管真落库 + 发布收敛 `_convergeInst` + NODES active 从 SERVICES 派生 + doUpub 文案对齐 + 两列(去恒等)+ 跨 ns 样例 + 获取记录回源粒度。〔已派 proto-fixer 执行〕

## 跨仓与契约纪律(贯穿)

- **两仓同 PR**:动 worker↔agent 契约的改动,services-monorepo 与 cnp **同一 / 关联 PR**;cnp `sync-plugins.js` 文档 **pin commit + 负责人**。
- **contract test 是闸**:P0-2 是 P1/P2 回归闸,改契约先过它。
- **rolltest 床**(192.168.31.57 / openclaw)做各阶段 e2e。
- **安全取舍**:「安全 vs 部署省事」冲突一律偏部署省事;既有兜底(`validate_managed_dir` 等)保留,不新增强制安全配置。

## 现状速查(哪些是新建 / 改 / 已有)

- **零实现 / 全新**:agent 插件缓存(✓)、worker-facing 端点、docker 含 stopped 采集、**agent 周期发现上报线程**、console 接收/DiscoveredNode 表/实例页、**ServiceImage 台账**、跨 agent 协调器、跨机聚合查询、desired-state 发布 UI。
- **改既有**(合并后均在 **service-console**):`rolling.py:_run_rolling`(unmatched 守卫)、`releases.py`(publish 触发 + 跨机扇出)、`distribution.py`(审计口径)、**`nodes.py`(寻址迁 DiscoveredNode + 进程内直调 + 保 degraded 不变式)**、`instance_match.py`(补 composeProject 上报);cnp `sync-plugins`(配置 + mode2 gate)。**Phase M 把 `hub_client.py` 改造为进程内 async 适配器**(保留 7 函数名 + 契约,函数体 await 调 app/hub handler,删 `SERVICE_HUB_URL`/`HUB_ADMIN_TOKEN` + httpx 依赖)。
- **复用不动 / 已有**:`distribution.py` 的 `query_plugins`/`download`(分发端点 + pull-token + IDOR)、安装链(curl→tar→copy→pm enable)、`validate_managed_dir` 安全闸;**实时日志全链路**(agent `core/log_sessions.py` + `app/routers/logs.py` SSE+fan-out + e2e)——node-control 已实现,合并后同进程,仅缺 console 实例页 UI(P3-9)。
