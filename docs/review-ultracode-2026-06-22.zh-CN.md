> 本文件由 ultracode 多 agent 对抗式评审自动产出(37 个 agent,原始发现 30 条,证伪核验确认 26 条,完整性遗漏 11 条)。
> 评审对象:统一开发计划 `plugin-distribution-dev-plan.zh-CN.md` + 平台原型 `plugin-platform-prototype.zh-CN.html`(对照设计 + service-console/hub/agent + cnp sync-plugins 真实代码)。
> 评审日期:2026-06-22。维度:原型-JS / 原型-UX / 计划-准确性vs代码 / 计划-排序可执行性 / 计划-与设计原型对齐 + 完整性 critic。

---

# 插件分发改造 + console 合并 · 评审汇总报告

> 范围:`services-monorepo` 的三份制品——设计冻结基线 `plugin-distribution-redesign.zh-CN.md`、唯一主计划 `plugin-distribution-dev-plan.zh-CN.md`、可点击原型 `plugin-platform-prototype.zh-CN.html`(均位于 `…/orchidea/services-monorepo/docs/`),并对照 `service-console` / `service-hub` / `service-agent` 与 `cnp/docker/nocobase/sync-plugins.js` 现网代码。本报告只纳入逐行核验为真(real=true)的发现 + critic 完整性缺口。

## ① 一句话总结论

**底层技术判定可信、不阻塞合并施工,但有两类必须先补的硬伤:计划存在 7 处"按它做完交付不出原型主界面 / 命中已冻结契约 / 会静默丢列或回退核心不变式"的完整性与排序缺口(集中在 S4 合 DB、S5 删 hub_client、P3/P4 控制面 UI、跨机服务数据模型),以及一组原型/设计文档与当天(2026-06-22)刚冻结的"hub+platform 合并""两列不恒等""单一真相"决策互相矛盾的演示态/文档一致性缺陷——M 段开工前应先解决 S4/S5 两条 High 与 P0-3/P0-4 的契约定稿,其余原型/文档缺陷可并入对应阶段。**

## ② 严重度计数表

| 严重度 | 已确认发现 | critic 缺口 | 小计 |
| --- | --- | --- | --- |
| Blocker | 0 | 0 | 0 |
| High | 2 | 6 | 8 |
| Medium | 7 | 4 | 11 |
| Low | 8 | 1 | 9 |
| 登记/反例(无需修, severity=None) | 1（VERIFIED-1） | — | 1 |
| **合计(去重前)** | 18 | 11 | 29 |

> 说明:发现严重度均采用 verifier 的 severityAdjusted（多条原报 High 因"纯原型/文档制品、无运行时/数据/安全风险"下调为 Medium/Low）。下文按调整后严重度并对重复项做了合并，**去重后实际待办约 18 项**（详见第⑤节）。

---

## ③ 按 High / Medium / Low 分组逐条（已合并重复项）

### High（8 条；其中 2 条来自发现、6 条来自 critic 缺口）

**H-1　S4「合 DB」会静默丢列 + 漏合 init_schema 守卫（合并:plan-ordering F1 + critic「grep __tablename__ 维度不足」）**
- 位置:`plugin-distribution-dev-plan.zh-CN.md:48`(S4);`service-console/app/db.py:16-25,42-54`、`service-hub/app/db.py:15,32-44`;`service-hub/migrations/versions/20260306_0002`(commands +original_request_id/retry_count)、`_0003`(agents +agent_key_hash/key_issued_at)、`20260620_0005`(commands +mode);`service-console/migrations/versions/20260619_0001_initial_schema.py:12-13`(注释自陈"照 hub 0001 手写、非 autogenerate")。
- 问题:S4 验收只写「grep `__tablename__` 查无冲突 + 统一 db.py + 重生成单一 0001」。但两边表名两两不冲突、各 0 个 ForeignKey/relationship——表名根本不是真实风险点。真正会崩的两处都没进任务:① 两个 `db.py` 各有 `_managed_tables` 的 legacy-detection 守卫(console=8 张、hub=4 张),合并后必须并成 12 张全集并保留「部分初始化→RuntimeError」语义,否则一个"只有 hub 4 表的旧库"对 12 表全集做交集非空但子集不成立 → 被误判"部分初始化"拒绝升级;② hub 的 3 个增量迁移加了 5 个 delta 列(`commands.mode/retry_count/original_request_id`、`agents.agent_key_hash/key_issued_at`),且本仓既定惯例是**手写** `op.create_table`——若沿此惯例手工拼接两个旧 0001,会静默丢这 5 列,**新库 fresh-build 测试照绿、生产老库升级缺列**,而 `store.py` 运行时实读这 5 列(凭据校验/命令重试/mode 逻辑)直接报错。
- 建议:S4 拆两条显式子项并收紧验收——(a) 合并 `_managed_tables` 为 12 张全集、保留 legacy 守卫,加「旧库升级路径」迁移测试;(b) 明确「单一 0001 必须 `alembic autogenerate` against 合并后 `Base.metadata`,禁止手工拼接两个旧 0001」,验收加列集断言「新库列集 == console 0001 ∪ hub(0001+0002+0003+0005)」（尤校上述 5 列存在）；统一两库 `created_at/updated_at` 时区与 Base。把 S4 验收从「测试库按新迁移建」升级为「**老库 + 新库双路径建表均绿 + 列集断言**」。

**H-2　S5「删 hub_client.py」工作量被严重低估:7 个远程函数 / nodes.py 5 处调用点 / sync→async 边界 / degraded 不变式 / 测试桩全失效（合并:plan-ordering F3 + critic「S5 未列 nodes.py 5 调用点」）**
- 位置:`plugin-distribution-dev-plan.zh-CN.md:49`(S5)、`:133`;`service-console/app/hub_client.py:60-234`(7 函数:provision/list_agents/list_instances/dispatch_command/rolling_restart/list_commands/rotate_agent_key);`nodes.py:83,143,267,313,337,373`(5 处调用,releases.py 的 P4 才动);`nodes.py:13`(单 agent 卡死不拖垮整页的不变式)、`:22`(monkeypatch 桩注释);`service-hub/app/routers/rolling.py:38`(`hub_state.call_agent`,async+WS)。
- 问题:S5 一句「console 路由 `hub_client.*` → 直接调 hub store/逻辑;删 `hub_client.py`;改 `test_hub_client`」把风险写小了。① `nodes.py` 现用 `asyncio.to_thread` 包 **sync httpx**(带 timeout),`list_nodes` 的 fan-out 用 `gather(return_exceptions=True)+degraded` 实现「单 agent 卡死不拖垮整页」——改进程内直调后失败态从"跨进程 httpx timeout"变成"本进程内 await agent 经 WS 往返",超时/取消语义完全不同,**必须重新落 `asyncio.wait_for`**,否则单 agent WS 卡死会吊死整页(直接打破既有不变式);现有 degraded 用例用 stub 立即抛 `TimeoutException` 模拟,改进程内后根本测不到新失败态,「console 全量绿」这道闸兜不住。② `dispatch_command/rolling_restart` 的 `requested_by` 现由 hub 据 admin-token **服务端派生**,进程内绕过 HTTP 后这套 caller 身份派生要在 store 层重新接,否则静默丢操作审计 caller。③ `test_nodes.py` 约 34 处 + namespaces 相关测试全是 `monkeypatch.setattr(nodes.hub_client, …)`,删模块后全失效,测试改造量远不止 `test_hub_client` 一个文件。
- 建议:S5 展开为可分配子项 + 补验收——列出 7 个 `hub_client` 函数→对应 hub_state/store 内部方法的映射表(重点标 sync→async 边界);明确「进程内调用必须保留 per-agent 短超时(`asyncio.wait_for`)+ gather 隔离」并加回归「单 agent 卡死整页仍响应」;明确 `dispatch/rolling` 的 `requested_by/request_source` 进程内取值来源(不再有 admin token 派生);把 `test_nodes`/`test_hub_client`/namespaces 桩重写显式列入 S5。验收从「console 全量绿」补成「nodes 节点页 fan-out degraded 不变式回归 + caller 身份正确」。

**H-3　node-operations 寻址链仍是 Service-表-权威,无任务迁到 DiscoveredNode（critic 缺口;设计 §3.3 推翻 v3 的核心连带改动）**
- 位置:设计 `plugin-distribution-redesign.zh-CN.md:133`(§3.3 推翻 node-control v3:dir/镜像/容器寻址权威 Service 表→DiscoveredNode);`service-console/app/routers/nodes.py:198`(`_resolve_service` 按 `(namespace.code, service_code)` 反查 Service 取 `svc.dir/default_image/nacos_service_name`)、`:219`(`_compose_default_project`)、`:245`(`_derive_health_base_url`)、`:328/333`(`dispatch_node_action` redeploy 读 `svc.default_image`);dev-plan 全文 grep `nodes.py/_resolve_service/寻址权威` **0 命中**。
- 问题:`P0-4`(行34)已把「dir/镜像权威 = DiscoveredNode」定为冻结契约,`P3-4` 也只「建 DiscoveredNode 表 + 实例页 UI」,但**没有任何任务把现有 node-operations 下发链(启/停/更新/redeploy)从 `Service.dir` 迁到 `DiscoveredNode.dir`**。后果:DiscoveredNode 建好后实例运维仍按 `Service.dir` 寻址,与「干掉手配 dir」矛盾;且 `Service.dir/default_image` 按 P0-4 退化为 nullable 后,这些路径会因 `dir=None` 直接 400/409。
- 建议:P3 或 P4 增显式任务——重写 `nodes.py` 的 `_resolve_service/_derive_health_base_url/_compose_default_project/dispatch_node_action`,寻址源从 Service 行改为 DiscoveredNode 行(按 `agentId+nacosService` 或 `composeProject` 定位 dir/image/containerId),明确 Service.dir/default_image 退化后的取值来源与回退。验收补「Service.dir 为空但 DiscoveredNode 有 dir 时实例运维仍可下发」。

**H-4　「服务两管理面 + desired-state diff 发布」整套 UI 在原型/设计 §4.2 是核心交互,计划零任务（合并:plan-design F2 + critic「ServiceImage 台账无任务」+ critic「镜像配置管理面无任务」）**
- 位置:原型 `…prototype.zh-CN.html` 插件配置二级页(行330-342,`openServiceConfig/openBind/unbindPlugin/openChangeVersion`)、镜像配置二级页(行344-358,`SERVICE_IMAGES`/`rollbackImage` 行1137-1167)、统一 diff 发布(行595-712,`svcDiff/openDeploy/startDeploy`+变更摘要)、镜像漂移/对账(镜像)(`_nodeImgDrift` 行842、`openService` 行1069-1074);设计 §4.2(行188-209 两管理面 + desired-state apply)、§8(行292-293 新增 `service_plugins/service_plugin_version/ServiceImage` 模型)、§12(行363-364 自陈这两管理面 ❌待建);计划全文 grep `ServiceImage/镜像配置/desired-state/对账(镜像)` **仅命中 P4-2 的 pull-redeploy 协调器**,无 UI/台账任务。
- 问题:计划 P3 只建发现/实例页/纳管/日志,P4 只建后端协调器与 publish 触发,P5 是审计/灰度/迁移。整条链没有任务负责:① 服务「插件配置」二级页 UI(后端模型多已存在,缺的是 UI);② 服务「镜像配置」二级页 UI **+ ServiceImage 历史台账表 + 迁移 + store/路由 + 回滚→pull-redeploy**(模型与 UI 双缺);③ desired-state「一个发布按 diff 自动选 restart/pull-redeploy」的发布弹窗 + 变更摘要;④ 镜像漂移/对账(镜像)纳入实例页与服务对账。按计划做完仍交付不出原型展示的主界面。`nodes.py` redeploy 现读 `svc.default_image`,也应改读 ServiceImage 当前行(与 H-3 退化任务对齐)。
- 建议:在 P3/P4 补四条任务(每条标 service-console SPA + 对应后端模型)——① 插件配置二级页 UI;② 镜像配置二级页 UI + `db_models` 加 ServiceImage 表 + 迁移 + store/路由 + 回滚 pull-redeploy;③ 统一 desired-state 发布弹窗(暂存意图→svcDiff→按 diff 选机制 + 变更摘要);④ 镜像漂移入实例页/服务对账(镜像)。

**H-5　「跨机服务=同 nacos_service_name 跨多 ns/agent」与「平台 active 版本是单一真相」数据模型冲突,无任务调和（critic 缺口;设计自相矛盾）**
- 位置:设计 §4(行158「平台 active 版本是单一真相」)vs §4.1/M12(行182「跨多台机逻辑服务=同 nacos_service_name 在多个 agent/ns 各有实例;ns↔agent 1:1,跨机即跨 ns」);`service-console/app/db_models.py:51`(Service 唯一键 `(namespace_id, service_code)`)、`:118`(`ServicePluginVersion` 单活键 `spv_active_key=f'{service_id}-{plugin_id}'`,per-Service=per-ns);`releases.py:128`(publish 只对单个 `(service_id,plugin_id)` 置活);`P4-2`(行103「active 变更→触发受影响 **service**(单数)滚动」)。
- 问题:一个跨 3 机逻辑服务 = 3 ns = 3 个 Service 行 = 3 套独立 active,「单一真相」在数据模型上**不成立**。无任务处理「跨机服务一次 publish 须把同版本扇出到 N 个 ns-Service 行的 active」,也无「按 nacos_service_name 聚合时 N 行 active 不一致怎么算」。这是设计自身的矛盾,会直接传导到 P4 实现。
- 建议:P0/P4 增任务明确跨机服务发布模型——要么 publish 按 `nacos_service_name` 找出所有 ns-Service 行**事务性同步置活**(并定义部分失败语义),要么显式声明 active 是 per-ns、跨机一致性靠投放协调器保证、对账按 `nacos_service_name` 检 N 行 active 不一致告警;同时消除 §4「单一真相」与 §4.1 措辞冲突。

**H-6　agent 侧「周期发现 + 经 WS 主动上报」缺驱动它的调度线程任务（critic 缺口;现 agent 无任何出站定时器）**
- 位置:设计 §3.1(行104-113 要求 agent「每 N 秒」跑 nacos+docker 发现并经 WS 上报);`service-agent/agent.py:10-15`(只起 health server + WS connect 重连)、`core/ws_client.py:91-102`(只有反应式 `_on_message` + `_start_heartbeat` 心跳循环);grep agent 出站 `setInterval/schedule/Timer/periodic` 仅命中 heartbeat 与 reconnect。
- 问题:真实 agent 是**纯被动模型**。P3-1 建 docker 含 stopped 采集器、P3-2 建 instance_match 上报字段、P3-3 建 console 接收端 + 新消息类型,但**无一条任务负责 agent 端「周期调度线程 + 组装节点清单 + 经 ws send 主动上报」这个把采集器和 WS 串起来的循环本体**(类比 `_start_heartbeat` 但发 discovery)。且 `ws_client` 当前未对非-handler 代码暴露稳定的「主动发消息」入口。
- 建议:P3 增一条 agent 任务——新增周期发现上报线程(参 `ws_client._start_heartbeat` 模式),每 N 秒调 docker(含 stopped)+nacos+instance_match 组装清单,经当前 WS 连接 send 一条 discovery 上报;明确该线程与 WS 重连生命周期耦合(连上才报、断线暂停)及上报消息 type 名(与 P3-3 新消息类型对齐)。

**H-7　原型「获取记录」展示 per-实例 + per-插件 命中/回源,但设计 §2.5 与真实 fetch_record 只能做到回源粒度——前端会按做不出的数据建 UI（critic 缺口）**
- 位置:原型 FETCH 假数据(行386-390、1169-1172,每行=一个实例容器、每插件标命中/回源);设计 §2.5(行95-98,平台 fetch_record=回源粒度 ns/service,同主机共享缓存的其余 N-1 容器平台不可见,per-container 以 agent 本地日志为权威);`service-console/app/routers/distribution.py:82-89`(`query_plugins` 对每个 active 插件无条件写一行 fetch_record,**无 caller/container 维度、无命中/回源标志**)、`db_models.py:124`(`FetchRecord` 也无这些列)。
- 问题:原型这页后端结构上**产不出**。P5-1 只说 fetch_record=回源粒度、caller 取 agent 反查,**未把 per-container 落到 agent 本地审计的查询/上报路径任务化,也未让原型改成回源粒度口径**。
- 建议:二选一并任务化——(a) 原型「获取记录」改为回源粒度(ns/service + 回源版本/时间),去 per-实例 + 命中/回源列;(b) 若坚持 per-container,新增 agent 本地审计落库 + 上报/查询端点任务(设计 §7 提过但无 P-任务),并在数据模型加 caller/容器/命中标志。P5-1 需明确选哪条。

---

### Medium（11 条;含原型演示态自相矛盾一组 + 排序/对齐若干）

**M-1　「一键纳管」死按钮 + 已纳管表纯静态,纳管/对账闭环断裂（合并:UX-loop F1 假成功 + UX-loop F2 wms-prod 数字矛盾 + JS F2 managed 表不刷新）**
- 位置:`…prototype.zh-CN.html` `saveService`(行582,只 toast 不写 SERVICES/不删 INBOX)、静态 managed 表(行288-303,tbody 无 id、3 行写死、无任何 render 重建)、`renderServicesPage`(行1044-1057,只刷统计卡 + inbox)、`openServiceConfig→renderServicePlugins`(行1112-1113)、wms-prod 静态行(行297-298「6 插件/0/4 实例」)vs `SERVICES.wms-prod`(行1027-1028,仅 2 插件、inst:[])vs NODES(行835-838,仅 2 个 stale 节点)。
- 问题:服务页主打「一键纳管」CTA(banner 行270-272),但 `saveService` 点完什么都不真正发生——inbox 行仍在、managed 不长新行、服务从未进 SERVICES 导致随后「去配插件」也配不了,却给成功 toast。同时 managed 表全程不被任何 render 重建:doBind/unbindPlugin/doUpub 增删插件、`_convergeInst/syncInstance` 把 recon 改「正常」后,详情抽屉与列表两处视图打架(详情说"正常/2 插件/2 实例",列表仍写"版本漂移/6 插件/0-4");wms-prod 三处口径(列表 6 插件/纳管 4 vs 详情 2 插件/0-2 实例 vs 配置 2 插件)互相矛盾。对账/纳管正是本次重设计核心价值,演示态自相矛盾损害可信度。
- 建议:managed 表改数据驱动(类 `renderInbox`,从 `_nsServices()`/SERVICES 渲染 tbody,行带 `data-ns`;已绑插件取 `plugins.length`、对账取 `s.recon/reconCls`、实例数按 `s.inst`+NODES 实算,与详情/统计卡同源);`saveService` 在 adopt 模式下 push 一条到 SERVICES + 从 INBOX 删对应项 + 重渲染 managed/inbox/统计卡/角标后再 toast;一并修掉 wms-prod 假数据。否则至少把 toast 改诚实文案并在真正落库前禁用确定按钮。

**M-2　正常「发布」(startDeploy/commitDeploy)跑完不收敛实例与对账,只有边角的重投/同步才闭环（JS F1）**
- 位置:`…prototype.zh-CN.html:699-712`(startDeploy)、`:755`(commitDeploy 只更新 `deployedImage/deployedPlugins` 两个意图快照);对照 `:726-733`(`_convergeInst` 收敛全部)、`:743-754`(syncInstance)。
- 问题:主路径「发布」的 done 回调(行708)只调 `commitDeploy(svc)`,而 commitDeploy 完全不动 ① `NODES[k].plugins[*][2]`(实例实际版本)② `NODES[k].image` ③ `SERVICES[svc].inst[*]` 的版本/ok 标志 ④ `recon/reconCls`——这些恰是漂移判据所依赖的字段。实证:改 wms-test-admin 插件版本后「发布」、动画完成 + toast「发布完成」,但 wms-test-admin2 纹丝不动 → 实例页仍标「版本落后」、服务详情仍标「版本漂移」。主功能闭环失败,只有 `_convergeInst`(强制重投)/syncInstance(同步)才收敛。
- 建议:把 `_convergeInst` 的收敛逻辑并入发布成功回调——startDeploy 的 done 里在 `commitDeploy(svc)` 之后调 `_convergeInst(svc)`(或抽公共 `converge` 函数),使发布后 NODES 实际版本/镜像、SERVICES.inst、recon 一并收敛,并 `renderNodes()+renderServicesPage()` 刷新两处。

**M-3　NODES[*].plugins 的 active(索引1)与 SERVICES[*].plugins 完全脱节,漂移判据是"实例内部 active vs 实际"而非"服务意图 vs 实例实际"（JS F3）**
- 位置:`…prototype.zh-CN.html:1133`(pickVersion 只改 SERVICES)、`:825-839`(NODES 三元组 `[name,active,actual]`)、`:1020-1029`(SERVICES 二元组 `[name,version]`)、`:801/813/857`(漂移判据 `p[1]!==p[2]`);全文无任何代码写 `NODES[k].plugins[*][1]`。
- 问题:两份独立种子。改服务插件版本只动 SERVICES,从不回写 NODES active,故 ① 改版本并发布后实例详情 active 列仍显旧版;② 真正的服务-实例版本错配不被指示器反映。强力佐证:同文件镜像漂移 `_nodeImgDrift`(行842)正确用了「NODES.image vs SERVICES.image(实际 vs 意图)」范式,作者明知正确范式却对插件用了次级的内部比较——这种不对称反证缺陷,且与 redesign「平台 active 版本是单一真相」(行158)直接冲突。后续 P4 实现以此原型为参考会把错误数据模型带进去。
- 建议:让 `NODES[k].plugins[*][1]` 从 SERVICES 派生(渲染时按 svc 查表覆盖),漂移判据统一改「实例实际 vs 服务意图」;或在发布收敛(见 M-2)时把 active 同步为服务意图版本。可直接复用现成镜像漂移范式。

**M-4　上传后「发布」(doUpub)违反全站「暂存→发布生效」模型,假称已触发投放（UX-loop F4）**
- 位置:`…prototype.zh-CN.html:1231-1237`(doUpub)vs `:1096-1106/1115-1136`(bind/unbind/changeVersion 均 toast「已暂存…去发布生效」)。
- 问题:全站统一 desired-state,真正下发要走 openDeploy 逐实例动画 + commitDeploy。但 doUpub 直接 `SERVICES[svc].plugins.push` 后 toast「已发布 X@ver → svc(已绑定并触发投放)」——既不跑 `_rollout` 也不 commitDeploy,只悄悄制造 pending diff。两处问题:文案假成功 + 与其余入口心智模型冲突(用户以为已上线,下次去看仍「待发布」)。
- 建议:doUpub 与其它写操作对齐——push 后改 toast「已暂存绑定 X(去发布生效)」并跳服务配置/发布;或若确要「上传即发布」,则在弹窗走 openDeploy 同款 diff+逐实例动画+commit,而非裸 push+假文案。

**M-5　设计文档仍写「platform + hub 两服务」,未反映当天冻结的合并决策（plan-design F1）**
- 位置:设计 `…redesign.zh-CN.md` §1(行21-23 架构图、42-48 组件表)、§4(行13/157 控制链)、§4.1(行168/180「均在 hub」)、§5(行245)、§8(行285/294「platform:」/「hub:」分节)、§11(行345-346)、§12(行357-365);vs 计划行6/12/39「彻底合并 hub+platform → service-console」。
- 问题:计划已把合并定为冻结决策(行6,2026-06-22),所有任务落点 service-console、删 hub_client、进程内直调;但作为「设计冻结基线」的设计文档全文仍按两独立服务描述(独立组件/独立 admin token 凭据/独立 DB 分节/控制链 platform→hub WS),且只有 §4.3 的实时日志更正注、无任何合并注。两文档对「几个服务、谁调谁、几套凭据」给出互相矛盾的权威说法;尤其 §4.1「hub 协调器」在合并后应是 console 进程内逻辑(计划 P4-1/P4-2 已写 rolling.py 进程内直调)。代码层面 service-platform 目录已被 `git mv`(S1 ✓)更放大读者困惑。
- 建议:在设计文档增补一处与 §4.3 同等的合并决策注(2026-06-22),声明 hub+platform 合并为单一 service-console、控制链由跨进程 WS 退化为进程内直调、admin token 等内部项删除;并把 §1 组件表/§4.1「均在 hub」/§5 安全表/§8「hub:」分节/§12 现状表的 platform/hub 措辞收口到 service-console(可保留括注「原 hub 模块」)。否则至少在文档头部标注「服务拓扑以计划 M 阶段为准」。

**M-6　原型「serviceCode = nacosServiceName 恒等」提示与设计/计划/代码定稿的「两列、默认同值可分别编辑」直接冲突（plan-design F4;原报 Medium，verifier 下调 Low；此处取保守 Medium 提示）**
- 位置:原型行420-421「key 约定:serviceCode = nacosServiceName」、行291/293-297 单一「别名」列;vs 设计 §3.4(行138/153「删除恒等说法,nacos_service_name 现 P1 允许为空,恒等会让分发链拿到空 key」)、计划 P0-4(行34 两列)、`service-console/app/db_models.py:43`(`service_code` 非空唯一)/`:47`(`nacos_service_name` nullable)、`test_crud_service.py:48/57`(分别设/断言不同值)。
- 问题:绑定契约(P0-4 冻结)+ 设计 §3.4 + **已落地后端代码**三者均按两独立列实现,而原型新建/纳管抽屉仍硬写恒等 + 服务表只有一个「别名」列。verifier 已据"后端代码已正确建好两列、不存在让错误行为上线的路径"下调为 Low——本报告将其列为待修文档一致性项,但提示其与已冻结契约冲突、应及时修措辞免误导实现者。
- 建议:更新原型新建/纳管抽屉——去掉恒等提示,改两字段(`service_code`[分发] / `nacos_service_name`[发现/滚动],默认预填同值可分别编辑),与 §3.4/P0-4 对齐。若刻意按 §4.2(行208)在 UI 隐藏两列,则原型 hint 也应删恒等措辞,避免实现者把底层做成恒等。

**M-7　原型缺「同 nacosService 跨多 agent」样例,设计 §4.1/M12 明确要求否则跨机滚动无从验收（plan-design F5）**
- 位置:设计 §4.1(行166/173/182,M12「原型须补一个『同 nacosService 跨多 agent』样例服务,否则 §4.1 无从验收」);原型 SERVICES(行1020-1029)3 服务各落单一 ns/agent、NODES(行825-839)无任何服务跨两 ns;`startDeploy`(行702)只遍历单服务 inst,能满足 ≥2 实例失败演示的服务其实例全在同一 ns。
- 问题:ns↔agent 为 1:1(跨 agent=跨 ns)。原型所有样例服务都只落单一 ns,**跨机滚动场景根本不存在**,发布/对账/进度视图演示的恒是"同机多容器"串行,而非设计 §4.1 要的"跨 agent 串行 + 集群级健康门"——正是 §4.1 明确区分并排除的形态。集群级不变式在原型里与单机多容器无法区分。计划 P4-1/P4 又以「跨机顺序滚两实例」为验收门。
- 建议:原型补一个同 nacos 服务名跨 2 ns 的样例(如 rolltest 在 rolltest-agent 外再加一个 ns 的实例,或新增 tamagawa-wms 跨两 ns),使对账/进度/集群健康门能演示跨 agent 串行;或在 §4.1 撤回对原型的硬性要求改为文字说明。

**M-8　失败即停 + 回滚是原型完整路径,但 Rollout 记录表与回滚事务代码零实现,且 mode 未在 P4-1 前定稿（critic 缺口;原型锁死决策风险）**
- 位置:原型 `_rolloutFailed/_rollback/_retryRollout`(行651-672 完整演示失败标红→停后续→前 N-1 可回滚→重试);设计 P0-3(行33 冻结 Rollout 字段含 `mode=freeze|rollback`)、§4.1 G5(行186)、§8(行290);grep `Rollout` 跨 console/hub **0 命中**;`service-hub/app/routers/rolling.py:76-81`(_run_rolling 失败即停只把内存 nodes 标 skipped,**无「回滚已动实例到上一 deployed 版本」逻辑、无目标版本/实例序列持久化**)。
- 问题:P4-3 列了 Rollout 落库 + 按 mode 冻结/回滚,但 mode 二选一在 P0-3 仍待定稿,而 P4-1/P4-2 自动触发若早于 mode 定稿会落到没有收敛语义的失败态;原型已把回滚画死、后端 mode 未定,存在**原型锁死决策**风险。且 Rollout 表「上一 deployed 版本」取值来源不存在(当前无 per-实例已部署版本字段)。
- 建议:确保 P0-3 的失败收敛 mode 在 P4-1 前定稿并写进冻结基线;P4-3 明确「回滚已动实例」实现位置(进程内协调器逐实例 drain→拉上一 deployed 版本)并定义 Rollout 表「上一 deployed 版本」取值来源;若 mode 选 freeze 需同步改原型(去自动回滚按钮)。

**M-9　S2 WS 端点并入单进程:两个 main.py 的模块级单例 + lifespan 必须显式合并,S2 未点出（plan-ordering F4）**
- 位置:`plugin-distribution-dev-plan.zh-CN.md:46`(S2);`service-hub/app/routers/agent_ws.py:13-22`(`import app.main as main_module` 取 `hub_state/logger/_handle_agent_message`)、`service-hub/app/main.py:38-39,93-99`(hub 模块级 `database`+`hub_state`+lifespan 含 `hub_state.initialize`+`interrupt_running_rolling`)、`service-console/app/main.py:35,38-44`(console 模块级 `database`+lifespan 含 jwt 校验+`init_schema`)。
- 问题:S2 写「挂 /ws/agent + include hub 路由 + 启动初始化 hub store,验收 import 修通」,但低估了:① S2「不动 DB」期间有两个 `Database` 实例并存的过渡态,hub 路由仍引用 hub 侧 `main_module.database`;② 两个 lifespan 必须合并,**漏掉 hub 的 `interrupt_running_rolling` 会让重启后中断的滚动任务永不被标 interrupted**(具体正确性缺口);③ SessionGuard default-deny `/api/**` 需确认不误拦 /ws/agent(此项偏弱:WS 不走 /api 前缀且 BaseHTTPMiddleware 不处理 websocket scope,仅作待验项)。
- 建议:S2 补三条显式子项——合并模块级单例(`database/hub_state/logger` 落点唯一,沿用 console「单例唯一落点 + 函数内延迟 import」约定);合并两个 lifespan(务必保留 `hub_state.initialize`+`interrupt_running_rolling` 并加启动恢复回归);确认/补 WS 握手能过中间件的 e2e。验收从「import 修通」升级为「WS agent 能连入 + 启动恢复中断滚动」。

**M-10　agent 单上游收敛:WS_URL(连 hub)与 PLATFORM_URL(回源)合并后指向同一 console 却无任务对齐（plan-ordering F2）**
- 位置:`service-agent/config.py:21`(WS_URL 强制必填 71-72)、`:55`(PLATFORM_URL)、`core/ws_client.py:8,106`(用 WS_URL);计划行41/S2/P3-6 反复「agent 不动」。
- 问题:M 把 hub+platform 合并为单进程单端口后,这两个上游物理上变成同一台 console,但计划无任何任务去收敛这两个 env / 声明「二者须填同一 console」/ 引入单一 CONSOLE_URL,P5-5 只覆盖 worker→agent 一跳。**修正一处事实**:agent 现有 `.env.example`/`docker-compose.yml`/README 其实只有 WS_URL 一个上游地址,PLATFORM_URL 及整个 P1 分发配置块尚未进部署模板——故真实缺口是"P1/P2 落地时须把 PLATFORM_URL 补进 agent 部署模板并注明应与 WS_URL 同指一台 console",比发现描述的"模板已有两个分离地址"更欠缺。
- 建议:在 S7(镜像/部署收尾)或 P5 新增一条 agent 上游收敛任务——保留两键但在 config/README/compose 显式注明「合并后二者必须指向同一 service-console」,或引入单一 `CONSOLE_URL` 派生 ws:// 与 http://;**并把"补 PLATFORM_URL 进 agent 部署模板"一并纳入**。验收:rolltest 床上 agent 仅配一处 console 地址即可同时建 WS 连接 + 成功回源拉包。

**M-11　跨机协调器需「nacos 服务→在哪些 agent 有实例」聚合查询,该查询归属未任务化（critic 缺口）**
- 位置:设计 §4.1 落地前置 L1(行184 自陈 hub 当前没有该映射);P3-2/P3-3 建 DiscoveredNode(`agentId+composeProject+nacosService`)理论可聚合;P4-1(行102)只说 for-each-agent 串行 + 集群健康门,把"按 nacos_service_name 跨 agent 分组"当已有输入;`service-hub/app/routers/rolling.py:35`(`_run_rolling` 签名是单 `agent_id+service_name`,聚合逻辑不存在)。
- 问题:P3 产出与 P4 消费之间缺「协调器如何从 DiscoveredNode 查出某 nacos_service_name 对应 `{agent:[实例]}` 集合」这块拼图的归属。
- 建议:在 P4-1(或 P3-5 对账)增子任务——基于 DiscoveredNode 实现「按 nacos_service_name 聚合得 `{agent:[实例]}`」查询,作为跨 agent 协调器与三态对账的共同输入;明确读 DiscoveredNode 还是实时 list-instances。验收补「同 nacosService 跨 2 agent 能正确分组并串行滚」(与 M-7 原型补样例一并核)。

---

### Low（9 条;原型 UX 打磨 + 计划措辞 nit + 计划完整性轻缺口）

**L-1　syncInstance/_convergeInst 收敛时把实例版本对齐到「任一已对齐实例的版本」而非服务意图版本（JS F4）**
- 位置:`…prototype.zh-CN.html:728-729/749-750`,`good=(s.inst.filter(i=>i[2])[0]||[])[1]`;`:1074`(i[1] 会显示)。
- 问题:服务无任何已对齐实例时 `good=undefined`,`if(good)row[1]=good` 跳过但 `row[2]=true` 照常 → "绿色已对齐标 + 旧版本号"不一致。样例数据每服务恰有一台 true 实例掩盖了它。修正:i[1] 是每实例单一代表版本标量(≈headline 插件 `s.plugins[0][1]`)。
- 建议:收敛目标改取服务意图版本(`s.plugins[0][1]`,而非整列表),good 为空时也能正确收敛。

**L-2　回滚/重投后服务页对账统计卡未刷新(_convergeInst/syncInstance 只调 renderNodes)（JS F6）**
- 位置:`…prototype.zh-CN.html:732/751`(收敛后仅 `renderNodes()`)、`:1044-1057`(`renderServicesPage` 的漂移统计卡 `svcStat` 数据驱动、读 `i[2]` 标志)、`:765`(唯一刷新入口 `switchNs`)。
- 问题:收敛后切回服务页仍看陈旧漂移计数,需手动切 ns 才更新。注:服务列表「对账」列是静态表(M-1),补 `renderServicesPage()` 修不了对账列(那要先做 M-1 数据驱动改造);当前真正成立的独立缺陷只有统计卡。
- 建议:`_convergeInst`/`syncInstance` 收敛完成后除 `renderNodes()` 外补调 `renderServicesPage()`。

**L-3　openLog 不重置「行号」复选框 logShowNo,跨实例打开沿用上次开关态（JS F5）**
- 位置:`…prototype.zh-CN.html:953-963`(openLog 复位 `_logAuto`/`logAutoScroll` 但漏 `logShowNo`)、`:520/941`。
- 问题:轻微状态泄漏,与 `logAutoScroll` 被复位形成不对称;非致命(`renderLogLines` 带空值守卫,定时器生命周期正确无孤儿)。
- 建议:如需默认态打开,openLog 里一并 `logShowNo.checked=true`;否则可忽略。

**L-4　推荐路径「从发现一键纳管」入口藏在非默认 tab（UX-loop F6;原报 Medium，verifier 下调 Low 并证伪"主操作"措辞）**
- 位置:`…prototype.zh-CN.html:269-278`(服务页默认 managed tab)、`:271`(手动预建按钮 `class="btn"` 次级样式,**非** `btn pri`)、`:277`(未纳管 tab 带黄色 t-warn 角标)。
- 问题:**修正措辞**——手动预建按钮已是次级描边样式(作者刻意降级),且未纳管 tab 的告警角标已起提示作用,"强引导手动预建/醒目主操作"被代码证伪。真实缺口仅:默认 managed tab 上无指向 N 个发现服务的显式 CTA、inbox>0 时不默认落 inbox。
- 建议:managed tab 加「去纳管 N 个发现服务」CTA(带 inbox 角标),或未纳管>0 时默认落未纳管 tab。属可发现性打磨。

**L-5　计划/设计未明确「漂移强制重投(reapply)与 diff 发布是两条路径」,原型按评审 H6/M14 已区分（plan-design F6）**
- 位置:原型迭代日志行29 + `openReapply/startReapply/syncInstance`(行713-754,无 diff 漂移→强制重投,不改意图、滚动 restart 收敛);计划 P4-2(行103)只覆盖 diff 方向「改 active→触发滚动」、P3-5(行91)只暴露漂移不定义修复动作;设计 §4.2(行209)只在单实例「更新」里隐含、未与 diff 发布并列。
- 问题:实现者可能只做 diff 发布,漏掉漂移修复入口(对账只暴露 version-drift 却无收敛动作)。
- 建议:P4 增补/在 P4-2 验收点明「强制重新投放(服务级 openReapply)/同步漂移实例(单实例 syncInstance),不改意图」;设计 §4.2 把「diff 发布」与「无 diff 漂移强制重投」明确为两条路径。

**L-6　计划缺「命名空间切换器」UI 任务,原型已实现并贯穿所有页过滤（plan-design F3;原报 Medium，verifier 下调 Low）**
- 位置:原型顶栏 `<select id="nsSwitch">`(行228)、`switchNs/filterPage/_nsNodes/_nsServices`(行756-795)、统计卡按 ns 收窄;计划全文 grep `命名空间切换/nsSwitch/switchNs` **0 命中**(行75 P2-1 是 agent 忽略 ns,无关);现有 SPA `AppShell.tsx:100-116` 顶栏只有用户/退出下拉、无 ns 切换器。
- 问题:按现状/按计划做的 console SPA 不会自带跨页 ns 切换器与过滤联动。下调 Low 因:这是原型多做的 UI 打磨,设计 IA 只描述页面、未把全局切换器列为冻结要求。
- 建议:P3 增一条 console SPA 顶栏 ns 切换器 + 各页(实例/服务/获取记录及统计卡)按当前 ns 过滤,或并入 P3-4 验收。

**L-7　原型 ns 切换器(per-ns 过滤)与「跨机服务跨 ns」IA 冲突,跨机服务被切散无聚合视图（critic 缺口;与 M-7/L-6 同源）**
- 位置:原型 `switchNs`(行763)/`filterPage`(行769 按 data-ns 过滤)把所有页按单一 ns 过滤;设计 §4.1/M12(行182)跨机服务聚合 key 是可跨 ns 的 nacos_service_name。
- 问题:选中某 ns 后跨机服务其余 ns 实例被隐藏,看不到完整集群健康;IA 层 per-ns 过滤与跨 ns 聚合服务概念并存却未调和。
- 建议:配合 M-7 补跨 ns 样例后,明确跨机服务在 ns 过滤下的展示规则(选某 ns 时是否仍整体显示该跨机服务 + 标注跨 ns,或提供 by-nacosService 聚合视图);P3-5 三态对账对跨 agent 同名服务按 nacos_service_name 聚合,UI 提供不被 ns 切散的入口。

**L-8　P2 验收依赖 sync-plugins.config.json,但 cnp 仓该文件不存在(仅 sync-plugins.js),P0-2 pin 对象缺落点（critic 缺口）**
- 位置:计划 P2-1(行75)/设计 §9(行311)假设改 cnp 的 `sync-plugins.config.json`;实查 `cnp/docker/nocobase/` 下只有 `sync-plugins.js`(脚本行78-81:配置不存在则 exit 0;默认路径 `{cwd}/sync-plugins.config.json` 可被 `ORCHISKY_CONFIG_FILE` 覆盖)——配置文件是运行时部署侧提供、不在仓库。
- 问题:P0-2 pin 的是 `sync-plugins.js`,但 P2 要改的 config 不在 cnp 仓,contract test 也测不到部署侧 config。P2「改 config.json」交付物归属不清。
- 建议:明确 P2-1 交付物——若 config 是部署侧运行时文件,则 P2 改的是部署模板/文档而非 cnp 仓文件,P0-2 pin 对象应是 `sync-plugins.js` + 配置样例(`sync-plugins.config.json.example`,§9 提到但仓内需确认存在);P5-5 迁移任务把「生成/下发新 config.json 到各 worker」作为显式步骤。

**L-9　两条计划措辞 nit（合并:plan-accuracy ACC-3 + plan-ordering F6 + plan-ordering F7;均 Low）**
- ACC-3:P3-2(行88)模块列写 `service-agent(instance_match.py)`,但该文件**已存在**(37 行,含 `match_instance`/`compose_project`,已被 `core/rolling.py:8` import 并读 composeProject)。建议标注为「改既有」与行132 全新清单区分。(误导风险已被 redesign §3.1/§12、review §76 多处"已有"标注缓解。)
- F6:P3-6(行92)写「**console** `app/routers/logs.py` 均已落地」,但该文件当前在 `service-hub/app/routers/logs.py`,M 后才进 console;设计 §365 权威标作「hub」。建议改「hub …logs.py(M 后并入 console)」或加一句「随 M/S2 从 service-hub 迁入」。(F6 误引行134——实际行134 写裸路径 `app/routers/logs.py` 并归因正确,证据一半不实。)
- F7:P4-1 前置「M, P3(实例→agent 映射)」粒度过粗(真正只依赖 P3-2/P3-3,不必等 P3-5 纳管/P3-6 日志 UI);且映射数据形状与 P4-1 消费格式间无契约门。建议把前置收窄为「P3-2+P3-3」,为「实例→agent 映射」补轻量契约(字段 `nacosService/host/agentId/composeProject/containerName`),与 M-11 聚合查询任务一并确认。

---

## ④ 完整性遗漏（critic 缺口汇总,已映射到上文条目）

critic 共 11 条缺口,**全部为真且无重复浪费**,按归属映射如下:
- 已提升为 **High** 独立条目:H-3(nodes.py Service→DiscoveredNode 寻址迁移)、H-5(跨机服务 vs 单一真相数据模型冲突)、H-6(agent 周期发现上报线程)、H-7(fetch_record per-container vs 回源粒度)。
- 已**合并进对应发现/任务**:critic「S5 nodes.py 5 调用点」→ H-2;critic「ServiceImage 台账/镜像配置无任务」→ H-4;critic「S4 grep 维度不足」→ H-1;critic「Rollout 零实现 + mode 未定稿」→ M-8;critic「nacos→agent 聚合查询归属」→ M-11;critic「ns 切换器 vs 跨 ns 冲突」→ L-7;critic「sync-plugins.config.json 不存在」→ L-8。

**贯穿性主题(供主评审决策)**:critic 缺口集中暴露了两个系统性问题——(1) **计划的"已有/待建/改既有"三态分类对 node-operations 下发链与 agent 出站调度有盲区**(H-3/H-6:把"建新表/建采集器"当任务,却漏了"把现有控制链/被动模型改成用新数据/主动上报"的连带改造);(2) **跨机服务(同 nacos 跨多 ns)这一核心生产形态在数据模型、发布扇出、聚合查询、原型样例、ns 过滤五处均未闭环**(H-5/M-7/M-11/L-7),是本次重设计最大的未收口主线。

---

## ⑤ 下一步建议（按"M 合并 S1-S8 前必须先解决"优先级排序）

### A. M 段(S2-S8)开工前必须先定稿/补的(阻塞合并质量)

1. **先定稿 P0-3 / P0-4 两条契约**(阻塞 P4 与 H-1/H-5/M-8):
   - P0-3:失败收敛 `mode`(freeze vs rollback)二选一定稿并写进冻结基线,**必须早于 P4-1**;定义 Rollout 表「上一 deployed 版本」取值来源(当前无 per-实例已部署版本字段)。
   - P0-4:把 H-5 的「跨机服务发布扇出语义」补进字段冻结——明确 active 是 per-ns 还是按 nacos_service_name 事务性同步置活,消除设计 §4「单一真相」与 §4.1「跨多 ns」的矛盾。
2. **改写 S4 验收门为"老库+新库双路径建表均绿 + 列集断言"**(H-1):合并 `_managed_tables` 为 12 张全集 + 保留 legacy 守卫 + 强制 `alembic autogenerate` 禁手工拼接两个旧 0001。这是会"测试绿、生产挂"的静默丢列风险,优先级最高。
3. **把 S5 展开为含 nodes.py 5 调用点映射 + sync→async wait_for + requested_by 落点 + 测试桩重写的子任务清单**(H-2):并把「单 agent 卡死整页仍响应」加为强制回归——否则合并后会静默回退节点页核心不变式。
4. **S2 补 lifespan 合并(保留 `interrupt_running_rolling`)+ 模块级单例唯一化子项**(M-9):验收升级为「WS agent 能连入 + 启动恢复中断滚动」。

### B. M 之后、P3/P4 排期时必须补的任务(否则交付不出主界面)

5. **补 H-3 / H-4 / H-6 / H-7 四条 High 任务**:node-operations 寻址迁到 DiscoveredNode、服务两管理面+ServiceImage 台账+desired-state 发布 UI、agent 周期发现上报线程、fetch_record 口径二选一并任务化。这四条决定"按计划做完能否交付原型展示的实例页/服务页/获取记录主界面"。
6. **补 M-11 跨机聚合查询 + 收窄 P4-1 前置粒度并加映射契约门(L-9/F7)**:作为 P4 协调器与三态对账的共同输入前置。

### C. 设计/原型文档同步(可与施工并行,但应在对应阶段交付前完成)

7. **设计文档补合并决策注、收口 platform/hub 措辞**(M-5);**修原型恒等措辞为两列**(M-6)——这两条直接撞已冻结契约,优先于其它文档项。
8. **原型补「同 nacosService 跨 2 ns」样例**(M-7),使跨机滚动/集群健康门/对账可演示;一并明确跨机服务在 ns 过滤下的展示规则(L-7)。
9. **原型演示态自洽修复一组**(M-1/M-2/M-3/M-4/L-1/L-2):managed 表数据驱动 + 一键纳管真落库 + 发布路径调 `_convergeInst` 收敛 + active 从 SERVICES 派生 + doUpub 文案对齐 + 收敛后补 `renderServicesPage()`。这组虽全是 Low/Medium 演示态缺陷,但因"对账/纳管/发布"正是本次售卖的核心闭环,**建议作为原型定稿的验收门一次性清掉**,否则演示时多处自相矛盾损害可信度。

### D. 可延后/可忽略

10. L-3(logShowNo)、L-4(纳管 CTA 可发现性)、L-6(ns 切换器 UI)、L-8(config.json 落点澄清)、L-9 的 ACC-3/F6 两条措辞 nit:随对应阶段顺手修即可,不阻塞。VERIFIED-1 为核验通过的反例登记(计划"已有/待建"分类整体可信),无需动作。

> **总体判断**:这是一份底层技术扎实(9 项关键"已有/待建"判定经逐行核验全部准确)、但在"合并连带改造 + 跨机服务主线 + 控制面 UI 任务化"三处有系统性完整性缺口的计划。**没有 Blocker,合并方向正确可以推进**;只要先把上文 A 组(P0-3/P0-4 定稿 + S4/S5/S2 任务展开)解决,M 段即可安全开工,B/C 组在 P3/P4 排期与文档定稿时补齐即可。