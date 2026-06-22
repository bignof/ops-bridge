# 插件分发改造 + console 合并 · 总开发计划

> **唯一主计划**(已并入原 `console-merge-plan`)。依据:`plugin-distribution-redesign.zh-CN.md`(设计冻结基线)+ `review-ultracode-2026-06-21.zh-CN.md`(评审收口)。
> 把设计 + 「hub/platform 合并为 service-console」翻成**可分配任务**,每条标 仓库 / 模块 / 前置 / 验收。
> **仓库**:`services-monorepo`(合并后只剩 **service-console** + **service-agent**)与 `cnp`(`docker/nocobase/sync-plugins.js`)。
> **决策(2026-06-22)**:彻底合并 hub+platform → **service-console**(一服务一 DB;DB 压成单一初始迁移);落点重命名 service-console;agent `plugin_cache` 保留。
> **基调**:agent 内网、不暴露外网;**部署简单 = 最高优先级**;安全类(H1/H2/M5/M6/M7/M9/L4)有意降级、不加部署摩擦(设计 §5)。

## 0. 阶段与依赖

```
P0 契约+冻结 ─┬─→ M  hub+platform 合并为 service-console (S1–S8) ──→ P3/P4/P5(console 控制面特性,进程内)
              └─→ A  agent 插件分发(P1 缓存 / worker-facing / 回源 + P2 worker)   [可与 M 并行]
P4 的 publish→自动滚动 依赖 §4.1 协调器(同批,不可早于它)
```

关键顺序:
- **P0 阻塞一切**(契约不定稿,两仓各写各的对不上)。
- **M(合并)尽早**——趁控制面特性未深化先合;**P3/P4/P5(控制面特性)排在 M 之后**,免得在 hub/platform 上建完又重并。
- **A(agent 侧)可与 M 并行**:agent 只对 console 的分发端点回源,合不合不影响 agent 自身;P2 依赖 P1。
- **P4 自动触发依赖 §4.1 协调器**(评审 M1/L1),二者同批。

**进度**:P0 设计冻结基线已出 · **P1-1 ✓**(commit 167cc65)· **M/S1 ✓**(commit 96c8c71)。

---

## P0 — 契约 + 设计冻结(阻塞后续)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P0-1 | **worker↔agent `/plugins` 契约定稿**:worker **零 ns**(agent 忽略传入 ns 恒用本 ns)、字段 `pluginName/version/url` 字面不变、`url` 指向 agent 自己、tokenless;关键行为=重定向跟随、version 优先、last-good、**空清单不清空**(评审 G2/M4/M5) | services-monorepo(契约文档)+ cnp(pin sync-plugins commit) | — | 契约文档冻结 |
| P0-2 | **contract test**:P0-1 关键行为写成断言,最小 fake-worker 在 agent e2e 跑;cnp 脚本 **pin commit + 负责人**,改契约两仓同 PR(评审 G1) | services-monorepo(agent e2e)+ cnp | P0-1 | contract test 绿;cnp 脚本 commit 已 pin |
| P0-3 | **失败即停回滚事务语义定稿**:失败后「冻结半迁移态等人工」vs「自动回滚已动实例」二选一;定义 `Rollout` 记录字段(目标版本、实例序列、各实例状态、失败点、mode)(评审 G5) | service-console(设计/数据模型) | — | 语义 + 字段冻结 |
| P0-4 | **字段语义冻结**:`service_code`(分发)≠ `nacos_service_name`(发现/滚动)两列;dir/镜像权威 = `DiscoveredNode`(**推翻 node-control v3**,评审 H4/H5);`agentKey` 生命周期(provision/存储 hash/校验/rotate,评审 G6) | service-console(`db_models.py`) | — | 数据模型契约冻结 |
| P0-5 | **安全取舍确认入冻结**:H1/H2/M5/M6/M7/M9/L4 内网降级、不加部署摩擦;保留 `validate_managed_dir` 兜底 | service-console(设计 §5) | — | 写入冻结基线 |

---

## M — hub + platform 合并为 service-console(S1–S8)

> 终态:单 FastAPI = platform 路由 + SPA + hub `/ws/agent` + hub 路由(logs/rolling/nodes)+ hub store;一个 DB + 一套 Alembic;删 `hub_client.py`(进程内直调);all-in-one 单进程;agent 不动。详见原 console-merge 内容,已并入本表。

| 步 | 内容 | 测试门 |
| --- | --- | --- |
| **S1 ✓** | 落点重命名 `git mv service-platform service-console`;修目录名引用(app/ci.yml);镜像/deploy 类留 S7 | console 测试 263 passed(已过) |
| **S2** | 并入 hub 代码:hub `app`(store/routers/ws/models/config/api_support/force_guard)并入 console;`main.py` 挂 `/ws/agent` + include hub 路由 + 启动初始化 hub store。先不删 hub 目录、不动 DB | console 启动 OK;hub 路由可访问;import 修通 |
| **S3** | 合 config:hub settings 并入 console 一份(`SERVICE_HUB_URL`/`HUB_ADMIN_TOKEN` 等内部项删) | config 测试绿 |
| **S4** | 合 DB + 迁移(**高风险**):合并 `db_models`(hub 表 + platform 表,先 grep `__tablename__` 查无冲突);统一 `db.py`(一个 engine/Base);删两套 `migrations/`,alembic 重生成单一 `0001_initial` 建全表 | 测试库按新迁移建;console 全量绿 |
| **S5** | 进程内直调(**高风险**):console 路由 `hub_client.*` → 直接调 hub store/逻辑;删 `hub_client.py` + 相关 config;改 `test_hub_client` 等 | console 全量绿 |
| **S6** | 合测试:hub tests 并入 console;修 import;原 platform+hub 用例(+调整)全绿 | 全量绿 |
| **S7** | 镜像/CI/nginx:all-in-one Dockerfile/supervisord 改单进程;nginx 单上游;`docker-publish.yml` 单 `service-console` 镜像、删 hub 镜像 job;`service-console/deploy/*` 单服务化 | 镜像构建通过;57 床冒烟 |
| **S8** | 清理:删 `service-hub` 旧目录;文档/README 指向 service-console;全量 + e2e(ws/logs/rolling) | 全量 + e2e 绿 |

**M 风险门**:S4/S5 最险,各完成后跑 console 全量;WS 整合后跑 `validate_logs_stream_e2e`/`validate_phase1_e2e`;旧目录到 S8 才删。

---

## P1 — agent 插件缓存 + worker-facing 端点(主战场;与 M 并行)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| **P1-1 ✓** | **agent 插件缓存模块**:内容寻址(键=`attachmentId`)、容量上限 + LRU、`PLUGIN_CACHE_DIR`、并发同包 per-key 锁 | service-agent `services/plugin_cache.py` | P0 | **已完成**(167cc65,7 例 + 全量 179 passed) |
| P1-2 | **worker-facing HTTP**:`GET /plugins?service=` + `GET /download/{attachmentId}`;**独立 server 绑 `PLUGIN_SERVE_HOST` 默认 `127.0.0.1`**(不复用 `HEALTH_HOST` 的 0.0.0.0)、`PLUGIN_SERVE_PORT`;**忽略 worker 传入 ns、恒用本 ns** | service-agent(新 server 模块) | P1-1 | 本机 curl 通;**从另一台 curl 被拒**(L4) |
| P1-3 | **agent→console 回源**:持 pull-token 调 console `/api/distribution/{plugins,download}`,清单 `url` 改写指向自己;回源喂 `plugin_cache.get_or_fetch` | service-agent + service-console(复用 `distribution.py`) | P1-1 | 未命中 → 回源一次 → 落缓存 → 喂 worker |
| **P1-4 ✓** | 配置项:`PLATFORM_URL`/`PULL_TOKEN`/`PLUGIN_NAMESPACE`/`PLUGIN_CACHE_DIR`/`PLUGIN_CACHE_MAX_BYTES`/`PLUGIN_SERVE_HOST`/`PLUGIN_SERVE_PORT` | service-agent `config.py` | — | **已完成**(随 P1-1) |

**P1 验收(rolltest 床)**:worker 经本机 agent 拉到插件,缓存命中 / 回源各一次。

---

## P2 — worker 改配置(cnp)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P2-1 | 改 `sync-plugins.config.json`:`adminUrl`→本机 agent、`apiPath`=`/plugins`、去 `adminToken`、**保留 `namespace` 字段**(agent 忽略其值) | cnp(`sync-plugins.config.json`) | P1 | 真 worker 从本机 agent 装插件成功 |
| P2-2 | **(条件)** worker 彻底无 ns:改 `sync-plugins.js` **mode2 gate**(去 `&& NAMESPACE` + 按需省略 URL)——**必改非可选**(评审 L3) | cnp(`docker/nocobase/sync-plugins.js`) | P1 | 不带 ns 仍能同步 |
| P2-3 | **验收补 M4**:(a) agent 可达返回空 → 不清空既有 `storage/plugins` + warning;(b) 单插件 download 失败 → 固化「隔离继续 vs 拦截启动」(建议首装必需插件缺失则 init 失败) | cnp + 验收用例 | P1 | 两类部分失败行为符合预期 |

**P2 验收**:worker 装插件成功;agent 挂走 last-good 不白屏;空清单不误清。

---

## P3 — 自动发现 + 实例页 + 实例实时日志(在 service-console,M 之后)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P3-1 | **docker「含 stopped」采集**(评审 H3,现零实现):新增 `docker ps -a`/`compose ls -a` + label 过滤;组装 `[{nacosService, composeProject, containerName, dir, image, host, running, healthy}]` | service-agent(`docker_cli.py`/发现) | P0 | 已停容器也能被发现/管理 |
| P3-2 | **instance→agent 映射 + 落位防错**(评审 M3):匹配带 `composeProject` 上报、console 校验唯一性;「一容器多实例/一实例多容器」冲突检测告警;钉清 nacos 注册端口语义 | service-agent(`instance_match.py`)+ service-console | P3-1 | admin/2admin 不张冠李戴 |
| P3-3 | **console 接收发现上报 + 落库 + 心跳**:**标 `stale`/`unknown` 不删行**(评审 M8),显式确认下线才删 | service-console(store + 新消息类型) | M, P3-1 | agent 失联节点保留可定位 |
| P3-4 | **DiscoveredNode 表 + 实例页 UI**:全自动 upsert;dir/镜像权威在此(P0-4) | service-console(`db_models.py` + SPA 实例页) | P3-3 | UI 自动出现节点,dir 自动填,无需手配 |
| P3-5 | **纳管模型**:发现 `nacosService` ∉ Service → 「已发现未纳管」收件箱;一键纳管 + 人工配插件;对账三态(running-but-unmanaged / managed-but-down / version-drift) | service-console | P3-4 | 收件箱 + 对账视图可用 |
| P3-6 | **实例实时日志 UI(§4.3)—— 后端已有,合并后同进程**:agent `core/log_sessions.py` + console `app/routers/logs.py`(SSE `POST /api/agents/{agentId}/logs/stream`,fan-out,e2e)**均已落地**;**本任务只做 console 实例页日志查看器 UI**:SPA 直连 console 自己的 SSE,dir 取自 DiscoveredNode、agentId 取自所属 agent;**复用既有 `sessionId/dir/logs_*` 协议,勿另造** | service-console(SPA;agent/后端不动) | M, P3-4 | 实例页看实时日志;关闭即断;离线实例不可用 |

**P3 验收**:UI 自动出现 admin/2admin 等节点(含已停);实例页接 SSE 看实时日志。

---

## P4 — 投放联动(service-console 进程内;依赖 §4.1 协调器)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P4-1 | **跨 agent 顺序协调器**(评审 M10/L1):`_run_rolling` 把「有 unmatched 就 abort」改成「过滤 unmatched、只滚本机 matched」(agent 无需改);加 for-each-agent **串行** + **集群级健康门**(滚 A 期间持续重探 B 健康、排除正在重启实例) | service-console(`rolling.py`) | M, P3(实例→agent 映射) | 跨机顺序滚、零中断、失败即停 |
| P4-2 | **publish/rollback 触发控制链**(评审 M1,现只改 DB active):active 变更 → 触发受影响 service 滚动(**进程内直调,不经 hub_client**);**restart / pull-redeploy 共用** P4-1 协调器(评审 M11) | service-console(`releases.py` + `rolling.py`) | M, P4-1 | 改 active → 自动滚 → 实例跑新版 |
| P4-3 | **失败即停 + 收敛**:`Rollout` 运行记录(P0-3)落库;失败→标记+停后续+按 mode 冻结/回滚;断点续投 | service-console | P4-1 | 注入失败 → 失败即停 + 收敛可见 |
| P4-4 | agent **预热**:console 通知「新版本将至」→ agent 提前回源缓存 | service-console + service-agent | P1 | 滚动时 worker 拉取零等待 |

**P4 验收**:改 active → 跨机顺序滚两实例都跑新版零中断;注入失败 → 失败即停 + 收敛。

---

## P5 — 加固 / 迁移(收尾,可并行)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P5-1 | **审计口径**(评审 M2/M9):console `fetch_record` = 回源粒度;per-container 以 agent 本地日志为权威;caller 取 agent 反查(源 IP→容器),不信 worker 自报 header | service-console + service-agent | P1 | 审计口径正确、不可伪造 |
| P5-2 | 灰度发布(只滚部分实例 / 按 DiscoveredNode 子集) | service-console | P4 | 可灰度 |
| P5-3 | 缓存 sha256 校验(**可选**,按 §5 取舍不阻塞、不强制) | service-console + agent | P1 | 启用后校验生效 |
| P5-4 | 可观测面板(agent /health 扩展、发布运行记录、发布→投放关联视图) | service-console + agent | P3/P4 | 面板齐全 |
| P5-5 | **迁移老节点**:root-JWT→pull-token、adminUrl→本机 agent | cnp + 部署 | P1/P2 | 老节点平滑迁入 |

---

## 跨仓与契约纪律(贯穿)

- **两仓同 PR**:动 worker↔agent 契约的改动,services-monorepo 与 cnp **同一 / 关联 PR**;cnp `sync-plugins.js` 文档 **pin commit + 负责人**。
- **contract test 是闸**:P0-2 是 P1/P2 回归闸,改契约先过它。
- **rolltest 床**(192.168.31.57 / openclaw)做各阶段 e2e。
- **安全取舍**:「安全 vs 部署省事」冲突一律偏部署省事;既有兜底(`validate_managed_dir` 等)保留,不新增强制安全配置。

## 现状速查(哪些是新建 / 改 / 已有)

- **零实现 / 全新**:agent 插件缓存(✓)、worker-facing 端点、docker 含 stopped 采集、发现上报、跨 agent 协调器、DiscoveredNode 表/实例页。
- **改既有**(合并后均在 **service-console**):`rolling.py:_run_rolling`(unmatched 守卫)、`releases.py`(publish 触发控制链)、`distribution.py`(审计口径/可选 sha256);cnp `sync-plugins`(配置 + mode2 gate)。**Phase M 删 `hub_client.py`**(改进程内直调)。
- **复用不动 / 已有**:`distribution.py` 的 `query_plugins`/`download`(分发端点 + pull-token + IDOR)、安装链(curl→tar→copy→pm enable)、`validate_managed_dir` 安全闸;**实时日志全链路**(agent `core/log_sessions.py` + `app/routers/logs.py` SSE+fan-out + e2e)——node-control 已实现,合并后同进程,仅缺 console 实例页 UI(P3-6)。
