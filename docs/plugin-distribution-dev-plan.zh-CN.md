# 插件分发改造 · 开发计划(任务拆解)

> 依据:`plugin-distribution-redesign.zh-CN.md`(设计冻结基线)+ `review-ultracode-2026-06-21.zh-CN.md`(评审收口)。
> 本文把设计的 P0–P5 翻成**可分配的开发任务**:每条标注 仓库 / 模块 / 前置依赖 / 验收口径。
> 涉及两个仓:**services-monorepo**(service-agent / service-hub / service-platform)与 **cnp**(`docker/nocobase/sync-plugins.js`)。
> **基调(已拍板)**:agent 内网、不暴露外网;**部署简单 = 最高优先级**;安全类(H1/H2/M5/M6/M7/M9/L4)有意降级、不加部署摩擦(见设计 §5「安全取舍」)。

## 0. 阶段依赖图

```
P0 契约+设计冻结  ──→ 阻塞所有后续
  │
  ├─→ P1 agent 缓存+worker-facing 端点 ──→ P2 worker 改配置
  │
  └─→ P3 自动发现+实例页+实例日志 ──→ P4 投放联动(含 §4.1 跨 agent 协调器)
                                          │
                                          └─ P4 的「publish→自动滚动」必须与 §4.1 协调器同批,不可早于它
  P5 加固/迁移  ──→ 收尾,任意时间并行(除迁移老节点须 P1/P2 就绪)
```

关键顺序约束:
- **P0 阻塞一切**:契约不定稿就开工 → 两仓各写各的、对不上。
- **P2 依赖 P1**:worker 改配置指向的 agent 端点得先存在。
- **P4 的自动触发依赖 §4.1 协调器**(评审 M1/L1):无协调器时让 publish 自动滚 → 撞「unmatched abort」守卫。二者同批。

---

## P0 — 契约 + 设计冻结(阻塞后续)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P0-1 | **worker↔agent `/plugins` 契约定稿**:worker **零 ns**(agent 忽略传入 ns 恒用本 ns)、字段 `pluginName/version/url` 字面不变、`url` 指向 agent 自己、tokenless;关键行为=重定向跟随、version 优先、last-good、**空清单不清空**(评审 G2/M4/M5) | services-monorepo(契约文档)+ cnp(pin sync-plugins commit) | — | 契约文档冻结;契约要点逐条列清 |
| P0-2 | **contract test**:把 P0-1 关键行为写成断言,最小 fake-worker 在 agent e2e 跑;cnp 侧脚本 **pin commit + 负责人**,改契约两仓同 PR(评审 G1) | services-monorepo(agent e2e)+ cnp | P0-1 | contract test 绿;cnp 脚本 commit 已 pin |
| P0-3 | **失败即停回滚事务语义定稿**:失败后「冻结半迁移态等人工」vs「自动回滚已动实例」二选一;定义 `Rollout` 记录字段(目标版本、实例序列、各实例状态、失败点、mode)(评审 G5) | services-monorepo(设计/数据模型) | — | 语义 + 字段冻结 |
| P0-4 | **字段语义冻结**:`service_code`(分发)≠ `nacos_service_name`(发现/滚动)两列;dir/镜像权威 = `DiscoveredNode`(**推翻 node-control v3**,评审 H4/H5);`agentKey` 生命周期(provision/存储 hash/校验/rotate,评审 G6) | services-monorepo(`db_models.py` 设计) | — | 数据模型契约冻结 |
| P0-5 | **安全取舍确认入冻结**:H1/H2/M5/M6/M7/M9/L4 内网降级、不加部署摩擦;保留 `validate_managed_dir` 兜底 | services-monorepo(设计 §5) | — | 写入冻结基线 |

---

## P1 — agent 插件缓存 + worker-facing 端点(主战场)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P1-1 | **agent 插件缓存模块**:内容寻址(键=`attachmentId`)、容量上限 + LRU、`PLUGIN_CACHE_DIR`(可挂卷)、并发同包 per-key 锁(借 `handlers._get_project_lock` 同款) | services-monorepo/service-agent(新 `plugin_cache`) | P0 | 同包二次取走缓存;并发不重复回源 |
| P1-2 | **worker-facing HTTP**:`GET /plugins?service=` + `GET /download/{attachmentId}`;**新增 `PLUGIN_SERVE_HOST` 默认 `127.0.0.1`**(不复用 `HEALTH_HOST` 的 0.0.0.0)、`PLUGIN_SERVE_PORT`;**忽略 worker 传入 ns、恒用本 ns** | service-agent(`core/health_server.py` 或独立 server) | P1-1 | 本机 curl 通;**从另一台 curl 该端口被拒**(L4 零成本默认) |
| P1-3 | **agent→platform 回源**:持 pull-token 调 `/api/distribution/{plugins,download}`,清单 `url` 改写指向自己;回源落缓存 | service-agent + service-platform(复用 `distribution.py`) | P1-1 | 未命中 → 回源一次 → 落缓存 → 喂 worker |
| P1-4 | 配置项落地:`PLATFORM_URL`/`PULL_TOKEN`/`PLUGIN_CACHE_DIR`/`PLUGIN_SERVE_HOST`/`PLUGIN_SERVE_PORT` | service-agent(`config.py`) | — | 配置可注入 |

**P1 验收(rolltest 床 e2e)**:worker 经本机 agent 拉到插件,缓存命中 / 回源各演示一次。

---

## P2 — worker 改配置(cnp)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P2-1 | 改 `sync-plugins.config.json`:`adminUrl`→本机 agent、`apiPath`=`/plugins`、去 `adminToken`、**保留 `namespace` 字段**(agent 忽略其值) | cnp(`sync-plugins.config.json`) | P1 | 真 worker 启动从本机 agent 装插件成功 |
| P2-2 | **(条件)** 若要 worker 彻底无 ns:改 `sync-plugins.js` **mode2 gate**(去 `&& NAMESPACE` + 按需省略 URL 拼接)——**必改非可选**(评审 L3) | cnp(`docker/nocobase/sync-plugins.js`) | P1 | 不带 ns 仍能同步 |
| P2-3 | **验收补 M4**:(a) agent 可达但返回空 → 不清空既有 `storage/plugins` + warning;(b) 单插件 download 失败 → 固化「隔离继续 vs 拦截启动」决策(建议首装必需插件缺失则 init 失败) | cnp + 验收用例 | P1 | 两类部分失败行为符合预期 |

**P2 验收**:真 worker 装插件成功;agent 挂走既有 last-good 不白屏;空清单不误清。

---

## P3 — 自动发现 + 实例页 + 实例实时日志

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P3-1 | **docker「含 stopped」采集**(评审 H3,现零实现):新增 `docker ps -a`/`compose ls -a` + label 过滤;周期发现组装 `[{nacosService, composeProject, containerName, dir, image, host, running, healthy}]` | service-agent(`docker_cli.py`/发现) | P0 | 已停容器也能被发现/管理 |
| P3-2 | **instance→agent 映射 + 落位防错**(评审 M3):匹配带 `composeProject` 一起上报、hub 校验唯一性;「一容器多实例/一实例多容器」冲突检测告警;钉清 nacos 注册端口语义 | service-agent(`instance_match.py`)+ hub | P3-1 | admin/2admin 不张冠李戴 |
| P3-3 | **hub 接收发现上报 + 落库 + 心跳**:**标 `stale`/`unknown` 不删行**(评审 M8),显式确认下线才删 | service-hub(store + 新消息类型) | P3-1 | agent 失联节点保留可定位 |
| P3-4 | **platform DiscoveredNode 表 + 实例页 UI**:全自动 upsert;dir/镜像权威在此(P0-4) | service-platform(`db_models.py` + SPA 实例页) | P3-3 | UI 自动出现节点,dir 自动填,无需手配 |
| P3-5 | **纳管模型**:发现 `nacosService` ∉ Service → 「已发现未纳管」收件箱;一键纳管(预填 ns+nacosName)+ 人工配插件;对账三态(running-but-unmanaged / managed-but-down / version-drift) | service-platform | P3-4 | 收件箱 + 对账视图可用 |
| P3-6 | **实例实时日志(评审/需求 §4.3)**:agent **流式命令通道**(`stream_logs` 跑 `docker logs --tail 500 -f` 逐行 yield;`log-stream-start/log-chunk/log-stream-stop` 带 streamId;停/断连/超时 kill 进程不留孤儿)+ hub 转发 + platform **SSE** 端点(`/api/instances/{id}/logs/stream`,admin)+ UI 查看器 | service-agent + service-hub + service-platform | P3-4 | 实例页看 `docker logs -f` 实时流;关闭即停无孤儿;离线实例不可用 |

**P3 验收**:UI 自动出现 admin/2admin 等节点(含已停);实例页能看实时日志。

---

## P4 — 投放联动(依赖 §4.1 协调器)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P4-1 | **hub 跨 agent 顺序协调器**(评审 M10/L1):`_run_rolling` 把「有 unmatched 就 abort」改成「过滤 unmatched、只滚本机 matched」(agent 无需改);加 for-each-agent **串行** + **集群级健康门**(滚 A 期间持续重探 B 健康、排除正在重启实例) | service-hub(`rolling.py`) | P3(实例→agent 映射) | 跨机顺序滚、零中断、失败即停 |
| P4-2 | **publish/rollback 触发控制链**(评审 M1,现只改 DB active):active 变更 → 触发受影响 service 滚动;**restart / pull-redeploy 共用** P4-1 协调器(评审 M11) | service-platform(`releases.py`)+ hub | P4-1 | 改 active → 自动滚 → 实例跑新版 |
| P4-3 | **失败即停 + 收敛**:`Rollout` 运行记录(P0-3)落库;失败→标记+停后续+按 mode 冻结/回滚;断点续投 | service-hub + service-platform | P4-1 | 注入失败 → 失败即停 + 收敛可见 |
| P4-4 | agent **预热**:platform 通知「新版本将至」→ agent 提前回源缓存 | service-platform + service-agent | P1 | 滚动时 worker 拉取零等待 |

**P4 验收**:改 active → 跨机顺序滚两实例都跑新版零中断;注入失败 → 失败即停 + 收敛。

---

## P5 — 加固 / 迁移(收尾,可并行)

| ID | 任务 | 仓库 / 模块 | 前置 | 验收 |
| --- | --- | --- | --- | --- |
| P5-1 | **审计口径**(评审 M2/M9):平台 `fetch_record` = 回源粒度;per-container 以 agent 本地日志为权威;caller 取 agent 反查(源 IP→容器),不信 worker 自报 header | service-platform + service-agent | P1 | 审计口径正确、不可伪造 |
| P5-2 | 灰度发布(发布只滚部分实例 / 按 DiscoveredNode 子集) | service-platform + hub | P4 | 可灰度 |
| P5-3 | 缓存 sha256 校验(**可选**,按 §5 取舍不阻塞、不强制) | service-platform + agent | P1 | 启用后校验生效 |
| P5-4 | 可观测面板(agent /health 扩展、发布运行记录、发布→投放关联视图) | 三端 | P3/P4 | 面板齐全 |
| P5-5 | **迁移老节点**:root-JWT→pull-token、adminUrl→本机 agent | cnp + 部署 | P1/P2 | 老节点平滑迁入 |

---

## 跨仓与契约纪律(贯穿)

- **两仓同 PR**:任何动 worker↔agent 契约的改动,services-monorepo 与 cnp **同一 PR / 关联 PR** 推进;cnp `sync-plugins.js` 在文档里 **pin commit + 负责人**。
- **contract test 是闸**:P0-2 的 contract test 是 P1/P2 的回归闸,改契约必须先过它。
- **rolltest 床**(192.168.31.57 / openclaw,见运维备忘)做各阶段 e2e 验收。
- **安全取舍**:遇到「安全 vs 部署省事」冲突一律偏部署省事;既有兜底(`validate_managed_dir` 等)保留,不新增强制安全配置。

## 现状速查(哪些是新建、哪些是改)

- **零实现 / 全新**:agent 插件缓存、worker-facing 端点、docker 含 stopped 采集、发现上报、跨 agent 协调器、实例实时日志流式通道、platform 节点表/实例页/SSE。
- **改既有**:`rolling.py:_run_rolling`(unmatched 守卫)、`releases.py`(publish 触发控制链)、cnp `sync-plugins`(配置 + mode2 gate)、`distribution.py`(审计口径/可选 sha256)。
- **复用不动**:`distribution.py` 的 `query_plugins`/`download`(分发端点 + pull-token + IDOR)、安装链(curl→tar→copy→pm enable)、`validate_managed_dir` 安全闸。
