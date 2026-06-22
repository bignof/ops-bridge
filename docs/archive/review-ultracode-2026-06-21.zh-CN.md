> 本文件由 ultracode 多 agent 对抗式评审自动产出(63 个 agent,原始发现 54 条,证伪核验确认 44 条,完整性遗漏 6 条)。
> 评审对象:`plugin-distribution-redesign.zh-CN.md` 设计文档 + `plugin-platform-prototype.zh-CN.html` 平台原型。
> 评审日期:2026-06-21。维度:设计-架构 / 设计-安全 / 设计-代码一致性 / 设计-内部一致性 / 原型-JS正确性 / 原型-UX / 原型-设计对齐 + 完整性 critic。

---

# 评审报告:插件分发设计 + 平台原型(ultracode)

## 总览

**一句话结论**:方案方向正确(desired-state apply + 自动发现 + 同主机一次回源喂多容器),但当前实现与设计宣称的「已有积木」存在系统性高估,且存在两条 RCE 级安全缺口和多处与前一天定稿文档的硬冲突,**尚不可作为开工依据,需先收口契约与安全不变式**。

**各严重度计数**(以核验修正后为准,已合并重复):

| 严重度 | 数量 | 概述 |
| --- | --- | --- |
| Blocker | 0 | 无单点「实现即崩」的阻断项,但下列 High 中两条安全项实质达到「不修不能上生产」 |
| High | 6 | 2 条 RCE 级安全(0.0.0.0 监听、包无校验)+ 1 条采集器零实现 + 2 条与已定稿文档硬冲突 + 1 条核心闭环 UI 断裂 |
| Medium | 16 | 架构闭环缺口、安全越权面、代码定位错误、原型 JS bug、设计↔原型不对齐 |
| Low | 13 | 术语漂移、文案不一致、暗色样式漏覆盖、原型小 bug、凭据叙事缺口 |
| 完整性遗漏 | 6 | 跨仓交付路径、契约自相矛盾、失败回滚语义、单 ns 视图、agentKey 生命周期 |

**整体成熟度判断**:**设计骨架成立,但「已有 vs 待建」的边界严重失真**——§12 现状对照表把多个核心采集/联动能力标为「✅ 已有」,实测为零实现或半实现(docker stopped 采集、publish→滚动触发、跨 agent 协调)。这会让 P2/P3/P4 排期系统性低估。**安全模型建立在两个未被代码强制的前提上**(127.0.0.1 监听、namespace 1:1),实际默认配置下击穿。**与 2026-06-18 / 2026-06-20 两份定稿设计存在字段语义级冲突**(serviceCode=nacosServiceName、dir 权威源反转),并存且互斥会让实现方无所适从。**原型作为评审载体,在最核心的「发现漂移→一键修复」闭环上自我证伪**(点进去全是「无变更」),反而掩盖了设计最看重的不变式。

建议:**先做一轮「契约 + 安全不变式」定稿(P0),再谈分期**。

---

## High

### H1. agent 入站 HTTP 默认 0.0.0.0,tokenless /plugins+/download 实际全网暴露
- **位置**:设计 §2.1(行53)、§5(行200);`service-agent/core/health_server.py:67`、`config.py:25`(`HEALTH_HOST` 默认 `'0.0.0.0'`)
- **问题**:整条 worker→agent「无 token」模型的唯一前提是「不出主机」(§5 行200 声称监听 127.0.0.1/本机网段)。但 §2.1 明示该入站 HTTP 是复用 health_server 的 `ThreadingHTTPServer`,而它绑 `HEALTH_HOST`,默认 `0.0.0.0`。若新 `/plugins`、`/download` 沿用同一绑定,则默认对所有网卡开放且无凭据,任意可路由到该端口者即可枚举 + 拉取该 namespace 全部插件包(含私有业务代码)。docker 端口发布场景尤甚。
- **建议**:方案写死硬约束——worker-facing HTTP 必须绑 127.0.0.1(或仅 docker 内网且不 publish),**新增独立 `PLUGIN_SERVE_HOST` 默认 127.0.0.1,不复用 `HEALTH_HOST`**。P1 验收加一条:从另一台机 curl 该端口应被拒。

### H2. 包字节全链路无 sha256/签名,被推迟为「可选 P5」;任一环被篡改即静默分发恶意插件并被执行
- **位置**:设计行84/224/268(sha256 标为可选/P5);`service-platform/app/routers/distribution.py:127-132`(裸流无校验)、`storage.py:85-88`(仅校验 name/version);全库 grep `sha256/checksum/digest/integrity` 在包路径 0 命中
- **问题**:.tgz 被 worker 解包 copy 到 `storage/plugins` 并对首装包 `pm enable`(§9 行241)= 服务端代码执行入口。全链路无内容完整性校验:agent 本机缓存目录(可挂卷)被污染、回源中间环节/平台存储被篡改/上传端投毒,worker 端均无从发现,直接安装运行——**RCE 级分发投毒**。§5「修掉 TLS MITM」只防传输,挡不住静态存储/缓存篡改与上传投毒。
- **建议**:把「attachment 入库记 sha256 + distribution 响应必带 sha256 + agent 落缓存与回 worker 前强制校验、不符即丢弃重拉 + worker 侧 sync-plugins 校验」**提到 P1(与缓存同期)**,定为安全不变式而非增强项。

### H3. 「docker 含 stopped 容器为主」是方案发现基石,但现有 agent 只能看到 running,该能力零实现
- **位置**:设计 §3.1 / §4.1 / §11 / §12(均称「docker(含 stopped)为主」,§12 标「✅ 已有」);`service-agent/services/docker_cli.py:11-21`(`docker ps -q`,仅 running)、`nacos_client.py:35-39`(仅 healthy and enabled)
- **问题**:方案把「nacos 只见在跑的 → docker 含 stopped 为主、nacos 补 health」当成既有积木和核心红利,但 docker 唯一列举入口只用 `docker ps -q`,agent 侧没有 `docker ps -a` / `docker compose ls -a`。后果:①「已停服务也能被管理(可 start)」这条卖点当前**零实现**;② DiscoveredNode 自动 upsert 的数据来源在「服务已停」时拿不到。这不是微调,是 P3 一个新的核心采集器,**直接决定「干掉手配」能否成立**。
- **建议**:docker_cli 新增 `list_all_containers`(`docker ps -a -q` 或 `docker compose ls -a` + label 过滤),按 `MANAGED_PROJECTS_ROOT`/compose label 过滤受管工程;**§12 把这条从「✅ 已有」改为「❌ 待建(P3)」**,否则 P3 排期严重低估。明确 stopped 节点在无 nacos 实例时 healthy/running 字段如何确定。

### H4. 把 dir/image 从 Service 挪到 DiscoveredNode,与前一天定稿的 node-control v3 正面冲突,废掉 compose 寻址权威源
- **位置**:设计 §3.4 行124/130、§8 行222、§4.2 行176;冲突文档 `2026-06-20-node-control-design.md:3,15-19`;`db_models.py:45-46`(`Service.dir`/`Service.default_image` 已存在)
- **问题**:本文称「dir/镜像/容器全来自 agent 发现,无手配」「Service 退化为纯逻辑层不再扛 dir」。但仅早一天的 node-control v3(副标题即「v3 核心修正」)明确把 `Service` 表作为 (agent×service) 权威源,「dir——权威路径来自台账」,并**把 v2「又把 dir 当自动发现」列为被修正的错误**。两份设计对「dir 是 Service 上手配的权威台账、还是 DiscoveredNode 自动发现的派生值」给出相反结论。dir 还牵涉 agent 安全闸(realpath 必须在 `MANAGED_PROJECTS_ROOT` 下、拒 self-project),不是可随意搬动的展示字段。
- **建议**:与 node-control v3 对齐后二选一并写清:(A) `Service.dir` 仍是 compose 寻址权威,DiscoveredNode 只存 agent **观测到的** working_dir 用于对账/漂移;或 (B) 若真改节点级自动发现,必须显式声明「本文推翻 node-control v3 §3 的 Service-表权威源模型」并说明安全闸如何重新落点。**两文档并存且互斥必须先消解**。

### H5. `serviceCode = nacosServiceName` 隐含等式与现有数据模型 + v3 设计直接矛盾,会击穿分发与滚动两条链
- **位置**:设计 §3.4 行139、§4.2 行176/184;`db_models.py:43,47`(两个独立 nullable 列);`2026-06-18-service-platform-design.md:59`(「serviceCode↔分发;nacosServiceName↔滚动(运维填/P1 nullable)」)
- **问题**:本文反复把 serviceCode 与 nacosServiceName 当同一 key。但现库二者是两个独立列、职责不同来源不同:`service_code` 是分发标识(`distribution.py:70-72` 按它查清单),`nacos_service_name` 运维手填、P1 允许为空(`rolling.py` 按它滚)。强令相等:① nacosServiceName 现 P1 可空,等同后分发链拿到空 key;② 既有行两列不等时,「对账 by nacosServiceName」直接 link 不上。这是把两个语义不同字段在文档里偷偷合并。
- **建议**:**删除「serviceCode = nacosServiceName」等式**。分发链用 `service_code`,滚动/发现 link 用 `nacos_service_name`(已存在)。若希望创建表单只敲一次,表述为「创建时 service_code 默认填入与 nacos_service_name 相同值,但仍是可分别编辑的两列」,而非声明恒等。

### H6. 概览「立即投放」与节点「投放」点进去都是「无待发布变更」,核心闭环 UI 断裂
- **位置**:原型 line 215 / 579-585 / 588 / 596 / 606 / 628-629 / 639
- **问题**:概览待处理区写「mom-wms 2.1.0 仅 1/2 实例」CTA「立即投放 →」,但 `initServiceState()` 把 `deployedPlugins/deployedImage` 初始化成与当前完全相等的副本,`svcDiff()` 永远 `none=true`,弹窗只剩次要的「强制重新投放」,主操作「确认发布」不出现。节点详情 wms-test-admin2(2.0.3 漂移)的「投放」同样死循环。**用户从最该一键解决的两个入口点进去,都被告知「没有变更可发布」**——平台主价值(发现漂移→一键修复)的直接断裂。
- **建议**:原型层让示例「待投放」服务的 `deployedPlugins/deployedImage` 真的落后于 `plugins/image`,使 `svcDiff` 产出真实 diff、弹「确认发布」主按钮并跑逐节点动画。产品层:**实例级漂移(active 已是目标但某实例没拉到)不该走「diff 发布」语义**,应直接走「强制重新投放/同步该实例」并明确文案。

---

## Medium

### M1. 「发布即触发滚动」是 desired-state apply 闭环核心,但 publish/rollback 当前不触发任何控制链
- **位置**:设计 §4 / §4.2;`app/routers/releases.py`(publish/reactivate/rollback 仅 `store.*` 改库);grep `rolling_restart|dispatch_command` 仅命中 `hub_client.py`/`nodes.py`,releases.py 零命中
- **问题**:三个写端点只改 DB `active` 行,真正调控制链的只有 nodes.py(人工对单 agent×service)。后果:改 active 版本后在跑 worker 不会自动重拉,要等下次有人手动重启/容器重建——正是「意图 vs 现实漂移」。方案把它轻描淡写为 P4 一行,实为 desired-state 成立与否的关键,且与 §4.1 跨 agent 协调强耦合(无协调器时自动触发反而会撞 §4.1 守卫失败)。
- **建议**:明确 P4 前的过渡语义——发布后 UI 必须显式标「待投放(version-drift)」并提供一键投放,别让用户误以为 publish 已生效;把「publish→滚动」自动触发**排到 §4.1 协调器之后(顺序依赖)**;§10 把 P4 标为核心价值依赖项而非可选增强。

### M2. 「同主机一次回源喂多容器」与「fetch_record 记到具体容器」天然冲突,平台审计永远缺缓存命中的 N-1 个容器
- **位置**:设计 §2.3 vs §2.5、§8;`distribution.py:79-89`(仅 agent 回源时写 fetch_record)
- **问题**:§2.5 要平台 fetch_record 记「哪个容器拉了哪个版本」并为此加 `X-Caller-Container`+caller 列;但 §2.3 核心红利是「同主机多容器拉一次喂多个 worker」。agent 命中本机缓存时不回源,平台只在「未命中那一次回源」写一行(带触发回源那个容器的 caller),其余共享缓存容器在平台审计完全不可见。**加 header + 一列拿不到全量 per-container 审计**。
- **建议**:明确分层——平台 fetch_record 语义降级为「namespace/service 维度回源审计」(本就是回源粒度);真正 per-container 审计以 **agent 本地日志为权威**,§7 给 agent 本地审计一个查询/上报路径。不要承诺平台 fetch_record 给出完整 per-container 视图。

### M3. admin/2admin 同主机两 compose 工程靠端口主键反查落位,可能张冠李戴
- **位置**:设计 §3.3;`services/instance_match.py:17-26`(端口主键 + bridge IP 兜底,无 compose project 校验);`core/rolling.py:40-41`(滚动路径才有 composeProject 漂移守卫)
- **问题**:`match_instance` 以宿主发布端口为主键、bridge IP 兜底。同机跑 admin+2admin 两工程时,若 nacos 注册的是容器内端口(常见 13000,两工程容器都存在),端口主键命中失败落到 IP 兜底;共用默认 bridge 网段时 IP 也可能撞。映射错一次,DiscoveredNode 的 dir/composeProject 就绑到错误工程,后续「更新/重部署」按错 dir 作用到另一组容器——**发现上报链没有滚动路径那层 composeProject 守卫**。
- **建议**:讲清 nacos 注册端口语义(宿主 vs 容器内);instance→container 映射成功后必须带 composeProject 一起上报、由 hub/platform 校验唯一性;**增加「一容器被多实例匹配」「一实例匹配到多容器」的冲突检测与告警,不要静默取第一个**。

### M4. 「空清单」与「单插件下载失败」两条路径下,worker 不是 last-good 而是悄悄少装插件
- **位置**:设计 §6/§9/§3.4;`docker/nocobase/sync-plugins.js:376-383`(空数组直接 return,无 last-good 对比)、`:412-415`(单插件失败仅计数隔离)、`:436-439`(任何情况 `exit 0`)
- **问题**:只有 fetchJson 抛错才走 last-good;agent 返回空数组(§3.4 自承「首启拉空」或半成品配置)命中「列表为空,跳过同步」直接 return,全新容器起来后零业务插件、仍可能白屏且无 warning;单插件下载失败被隔离成计数后脚本仍 `exit 0`,NocoBase 照常带着缺失插件跑。方案把这些归为「已具备、不重写」会让 P2 验收漏掉这两类最常见的部分失败。
- **建议**:P2 验收补两条:(a) agent 可达但返回空 → 断言 worker 保留既有 `storage/plugins` 不被清空且记 warning;(b) 清单非空但某插件 download 失败 → 固化「隔离继续 vs 拦截启动」的产品决策(建议首装必需插件缺失时让 init 失败)。**区分「agent 不可达(用 last-good)」与「agent 说你没有插件(可能误配)」两种语义**。

### M5. 安全边界依赖 namespace 1:1 但代码未强制,worker→agent 丢弃 service 作用域 → 同主机任意容器可越服务读整个 namespace
- **位置**:设计 §2.4 行89、§9 行250/257;`namespaces.py:75-99`(create 不强制 1:1);`store.py:557,595`(归属链仅按 namespace,download 不 key service)
- **问题**:授权边界完全寄托于「namespace.code = agentId 1:1」。但① create_namespace 对 1:1 无约束;② worker→agent 的 service 参数被 agent「校验/忽略即可」,worker 拿到的是 agent pull-token 在该 namespace 下能拉到的全集;③ 平台归属链只 key namespace。三者叠加:同主机任意容器(哪怕属另一 service 或被攻陷边车)向 tokenless agent 要插件,就能拿到该 namespace 下所有 service 的所有插件包。
- **建议**:二选一并写进方案——要么 agent 把 worker 传入的 service 作为必校验项并据此按 service 收窄回源(download 也补 service 归属校验);要么显式文档化「同主机 = 同 namespace 全插件共享信任域」并把 §2.4 容器来源校验定为强制项。另外 **create-namespace 对「一 agentId 仅绑一 namespace」加唯一约束**,别让 1:1 只是口头约定。

### M6. 镜像白名单默认空 = 放行全部,任意 image tag 可经 pull-redeploy 拉起到全机群(持宿主 docker.sock)
- **位置**:设计 §4.2/§8 行174/227/292;`services/compose.py:61-80` 与 `config.py:35`(allowlist 默认空=放行);`core/handlers.py:200-235`(update→compose pull,持 docker.sock)
- **问题**:新增镜像管理面让 admin 发布/回滚任意基础镜像 tag,落到 agent 即 `docker compose pull`,以宿主权限拉起任意镜像。唯一闸 `is_image_registry_allowed` 在 `IMAGE_REGISTRY_ALLOWLIST` 为空时放行全部。即默认部署下,任何进控制面者(admin token / XSS 复用会话)可把镜像改成任意 registry 任意镜像并一键 redeploy 到全机群,结合 docker.sock 直接拿宿主。
- **建议**:方案明确——**生产 `IMAGE_REGISTRY_ALLOWLIST` 必须非空(部署校验/启动告警)**,「镜像回滚/发布只能选 allowlist 内 tag」作为平台侧硬校验(BFF 拒绝越界 image),不要只靠 agent 末端兜底;回滚历史表也只存 allowlist 内 tag。

### M7. 自动发现把 dir/image 从「平台 Service 权威」反转为「agent 上报权威」,扩大可被污染的越权目录面
- **位置**:设计 §3.3/§8 行123-124/221;`nodes.py:192-198`(现状:平台 Service 表权威,BFF 不收客户端路径/image);`compose.py:13-43` 与 config.py(`validate_managed_dir` + `SELF_PROJECT_DIR` 默认空告警)
- **问题**:现状寻址权威源是平台 Service 表,BFF 显式「绝不接受客户端传路径或任意 image」。本方案把 DiscoveredNode(dir/image/composeProject)改为「全自动 upsert,全来自 agent 发现」并供控制链寻址。这把「compose 在哪个目录跑 down/up/pull」从平台静态配置变成经 agent WS 上报的数据;agent 被攻陷或通道被注入即可上报任意 dir/image,后续控制命令以该 dir 回打执行。末端 `validate_managed_dir` 兜底,但 `SELF_PROJECT_DIR` 默认空时自杀防护失效,且信任模型从「平台说了算」退化为「信 agent 上报」。
- **建议**:明确——**控制链执行命令的 dir/image 仍以平台侧策略(Service/ServiceImage 意图)为权威,DiscoveredNode 仅作展示/对账,不直接作为 compose 执行目录来源**;若必须用发现的 dir,则平台侧对 dir 白名单约束并要求 `SELF_PROJECT_DIR` 强制配置。把「发现数据不可直接驱动写操作寻址」写成不变式。(与 H4/H7 同源,合并处置)

### M8. agent 失联期间,DiscoveredNode 随 TTL 老化一并清除,使「已停也可管」自相矛盾;且该主机全部运维动作不可用
- **位置**:设计 §1+组件表(agent 三职合一)、§3.2(TTL 老化)、§8(heartbeatAt)、§3.1(已停可 start);`service-hub/app/store.py:1005-1020`(现有 `interrupt_running_rolling` 已示范「agent 没了需人工确认」)
- **问题**:DiscoveredNode 存活完全绑 agent 心跳。某主机 agent 挂超 TTL,该主机所有 DiscoveredNode 行被老化清除——包括「已停但希望仍能 start」的节点(§3.1 卖点)。即「agent 挂」让该主机全部节点从台账蒸发,恰是最需要人介入时台账却空了。此外 agent 是该主机唯一 docker 执行点,挂时 §4.2 启动/停止/更新全不可用,而 worker last-good 只覆盖「启动时拉插件」,不覆盖「agent 挂期间需重启/重部署」。
- **建议**:**把「agent 在线态」与「DiscoveredNode 存活」解耦**——agent 失联应把名下节点标 stale/unknown 并保留行(供人工 start 时仍能定位 dir/工程),只有显式确认主机下线才删;§6/§11 风险表补「agent 挂期间该主机 stop/restart/redeploy 全不可用」并给运维降级预案。

### M9. §2.5 用 tokenless worker 自报的 X-Caller-Container 作审计维度,该值攻击者完全可控,污染溯源
- **位置**:设计 §2.5 行95-96、§2.1 行51-53、§2.4 行90(容器来源校验仅 P2 可选);`distribution.py:81-89`(fetch_record 为权威审计)
- **问题**:caller header 由 tokenless 本机请求方任意填写,agent 无从验证(反查源 IP→容器那步是 P2 可选)。把未验证 caller 写进权威审计 fetch_record,等于让任何能访问 agent 者伪造审计身份;越权/异常拉取时溯源指向伪造容器名,与平台把 fetch_record 当权威审计冲突。
- **建议**:caller 维度**只取 agent 自身可验证的事实(源 IP→docker inspect 反查的容器/工程)**,不采信客户端自报 header;若保留 header 仅作辅助提示则在 schema/UI 标「未验证、不可作溯源依据」。把 §2.4 源 IP→容器校验从「P2 可选」提为审计可信前置条件。(与 M5 容器来源校验相关)

### M10. 文档把「unmatched 即失败」守卫归到 agent,实际在 hub `_run_rolling`;照文档改会改错文件
- **位置**:设计 §4.1 行152-155「要改两处」第①条;`service-hub/app/routers/rolling.py:46-48`(真正 abort 处)、`service-agent/core/rolling.py:24-55`(只回 matched 标志、从不失败)、`service-hub/tests/test_rolling.py:232`
- **问题**:文档描述「agent 的 rolling 见非本机就失败」并要求改 agent。实为:① 真正在 unmatched 上 abort 的是 hub 的 `_run_rolling`(有测试 `test_run_rolling_unmatched_aborts` 直接驱动 hub);② agent 的 `handle_list_instances` 从不失败,只逐实例回 `matched: true/false`——**agent 已经做了文档想让它做的事**。需要改的「过滤只滚本机」在 hub。
- **建议**:把「要改两处」第①条从「agent/rolling」改为「hub `_run_rolling`」(把 `if unmatched: fail` 改为过滤掉 unmatched、只滚本机 matched),agent 无需改;第②条「跨 agent 顺序协调器」也落在 hub。修正后整段「真实表现」改为「hub 的滚动编排器看到非本机实例就整体失败」。

### M11. §4.2 发布机制只覆盖 restart vs pull-redeploy「选哪个」,完全没接上 §4.1 跨 agent 顺序协调
- **位置**:设计 §4.1 行159-164(restart 的跨 agent 论证)、§4.2 行173/175/185(pull-redeploy 仅「逐节点」,无集群健康门论证)
- **问题**:§4.1 大篇幅论证 restart 需 hub 跨 agent 顺序 + 集群级健康不变式;但 §4.2 只说「仅插件变→restart;含镜像变→pull-redeploy」,把 restart 当已可用原子动作,对 pull-redeploy 是否也需同样跨 agent 协调 + 健康门只字未提(行173 仅括注「逐节点」)。pull-redeploy 中断更久、更需跨 agent 串行保活,却没有像 restart 那样论证多节点安全性。读者无法判断协调器是否覆盖 pull-redeploy。
- **建议**:§4.2 明确 **pull-redeploy 与 restart 共用 §4.1 的跨 agent 顺序协调 + 集群健康不变式**,只是单节点动作从 graceful-restart 换成 drain→pull→up;§4.1 标题/结论从「滚动重启」泛化为「跨 agent 顺序投放(restart / pull-redeploy 通用)」。

### M12. 原型:Service↔命名空间(=agent)1:1 建模,使 §4.1「一个服务跨多台机」无法表达
- **位置**:设计 §4.1 行150-164(跨 agent 串行协调)、§0.2 行10/§3.3 行126(ns=agent 1:1);原型 line 280 / 685-694(SERVICES 每项单 ns)/ 803
- **问题**:设计把「一个服务跨多台机、共用一个 nacos」定为「真正的多节点生产形态」并要求 hub 按 host→agent 分组串行协调。但原型每个 Service 只挂单一 ns 且 ns=agent 1:1,wms-prod 这种跨两台机服务只能拆成两个 Service 行,**§4.1 核心场景在数据模型层被消解**;三态对账也只能在单 agent 内算,跨 agent 同名服务的健康/漂移聚合无处落。
- **建议**:设计层先消解张力——要么「一个逻辑 Service 可关联多 agent/ns 的实例(`nacosServiceName` 跨 ns 聚合)」并改原型数据模型(SERVICES 增 instances→agent 维度);要么明确「跨机就是多 Service 行,§4.1 协调发生在 hub 而非 platform Service 层」。无论哪条,**原型必须补「同一 nacosService 跨多 agent」的样例服务**,否则 §4.1 无从验收。

### M13. 原型:发布进度视图无「集群健康 ≥1/quorum 门」「失败即停」,任何节点都必然走到完成
- **位置**:设计 §4.1 行161-163、§6 行207-208;原型 line 611-618(`startDeploy` 纯 setTimeout 串行,line 616 始终置 `done`,line 617 无失败分支)、line 447(审计页已有真实 `failed(drain 502)` 行)
- **问题**:原型用固定 `setTimeout(500)` 顺序跑死步骤,每节点最终都置「✓ 完成」并 commit,既不检查上一节点 ready 达标,也无任一节点失败→中止后续分支。讽刺的是操作审计页已有真实 `stop … failed(drain 502)`,说明失败是已知状态,但滚动视图呈现不出来——**恰好掩盖了设计最看重的「绝不同时重启、失败即停」不变式**。
- **建议**:`runNode` 加「健康门」与「失败注入」:某节点 drain/ready 失败→标红、停止后续、保留已完成节点为旧版,提示「集群健康跌破 ≥1 已中止」。对齐 §4.1 可验收语义,而非永远成功的乐观动画。

### M14. 原型:服务详情抽屉「发布」无离线守卫,可对离线/未起服务跑出「发布成功」
- **位置**:原型 line 338(列表入口已守卫 `toast('agent 离线，无法发布')`)、line 700(详情入口无守卫直调 `openDeploy('wms-prod')`)、line 612(`inst` 为空 fallback 伪造 `svc-1/svc-2`)、line 673/692/693
- **问题**:k8s-prod-a 离线,服务列表对 wms-prod 的「发布」正确拦截;但服务详情抽屉「发布」按钮无任何离线判断,进入 `openDeploy/startDeploy`,wms-prod `inst:[]` 触发 fallback 伪造实例名并播放整套 drain→重启→ready 动画、最后 toast「发布完成」。对 agent 离线、纳管未起(0/4)的服务演示出「成功逐节点投放」,与真实状态正相反——比「假成功 toast」更误导。
- **建议**:把离线判断收敛到 `openDeploy/startDeploy` 内部(按 `service.ns` 查 `AGENTS[ns].online`,离线则 toast 拦截);**删掉 `svc-1/svc-2` fallback**,`inst` 为空就报错;**抽出统一 `canDeploy(svc)` 判定**,列表/详情/节点抽屉/概览 CTA 共用,杜绝多处各判一次。(与 H6、原型完整性遗漏 G3 同源)

### M15. 多个操作只弹 toast 假成功、无后续状态变化,形成视觉死胡同
- **位置**:原型 line 205/242/281/320/322/405/410
- **问题**:刷新、重新发现、新建命名空间、忽略、选择文件/上传 点击后只 toast、DOM 不变。尤其「忽略 wms-test-portal」后行还在,违反「待处理→处理掉就消失」的 inbox 心智;「新建命名空间」作为主操作(`btn pri`)却连表单都不弹,是体验落差很大的死按钮。
- **建议**:有「列表语义」的操作产生可见反馈——忽略=移除该 tr 并把未纳管角标 -1;上传完成=往「最近上传」插一行;新建命名空间=复用抽屉弹最小表单(名称 + 生成 agentKey 的 show-once)。纯演示操作(刷新/重新发现)保留 toast 但改「进行中→完成」两态。

### M16. 原型:同一服务两实例展示两个不同镜像,但服务只有单一「当前镜像」,且对账三态不含镜像漂移
- **位置**:原型 line 627-629(同服务两实例镜像不同)、line 762-766(`SERVICE_IMAGES` 单 current)、line 333/342(对账仅插件维度「版本漂移」);设计 §8 行227(镜像是节点级,发现上报真实 tag)、§3.4 行135-138 三态
- **问题**:设计区分「ServiceImage = 应用哪个镜像的意图」与「节点级真实运行 tag」;三态 version-drift 仅针对插件 active 版本。原型给 wms-test-admin 两实例填了两个不同镜像,而镜像配置页只有一个「当前镜像」。结果:实例间镜像不一致(真实漂移)在 UI 上既不报警也无对账维度,只有插件版本漂移被标——**与设计「镜像节点级、要发现上报真实 tag」矛盾地呈现却不闭环**。
- **建议**:二选一——让两实例镜像在样例里一致(避免误导);或把「镜像漂移(节点实际 tag ≠ 服务意图 tag)」作为第四态纳入实例页/对账,镜像配置页「当前镜像」旁标「N/2 实例已应用」。

---

## Low

### L1. §4.1 跨 agent 顺序滚动方案自洽,但两块前置(发现链提供「实例→agent」映射、hub 串行协调器 + 集群健康门)尚未存在,且与 §4 自动触发循环依赖
- **位置**:设计 §4.1「依赖自动发现」;`service-hub/app/routers/rolling.py:38-39,48-54`、`service-agent/core/rolling.py:45-47`
- **问题**:§4.1 对现状判断准确、修法方向对,但三个落地盲点未点破:(1) hub 当前没有「nacos 服务 → 在哪些 agent 上有实例」的映射,这是硬前置依赖(§4.1 只末尾一句带过);(2) 把 agent 改成「只滚本机」后健康不变式从单机移到集群级,hub 必须在滚 A 期间持续探测 B 的健康,而现健康判定只用单次快照;(3) §4 想让 publish 自动触发,前提就是 §4.1 协调器,二者必须同批。
- **建议**:§4.1 显式标 hard-depends-on §3;协调器补「滚动期间跨 agent 实时健康门」(每滚完一个 agent 重新拉全集群健康再决定是否继续);明确 §4 自动触发与 §4.1 同批上线、不分期。

### L2. 周期发现与 compose 执行在同一 agent 并发跑,发现快照可能采到 down/up 瞬间,误判「节点消失/不健康」
- **位置**:设计 §3.1(周期发现并发读 docker)、§4.1(集群健康门)、§6(只提下载 per-key 锁);`core/ws_client.py:62-74`(每命令独立线程)、`handlers.py:38-45`(锁粒度=单 project dir,跨类型不互斥)/`:243-252`(down→up 全停窗口)
- **问题**:命令与发现是完全并发的两条读 docker 路径,无协调。发现刚好在某工程 down→up 窗口采样 → 上报 running=false/不健康 → 台账抖动、§4.1 健康门可能在自家滚动造成的瞬时下线上误判;若 §4 自动触发,滚动中的发现会把「正在被自己滚动」的实例算作不健康,与健康门互踩。§6 只提了下载幂等 per-key 锁,没覆盖「发现 vs 执行」并发。
- **建议**:发现上报对「本机正在执行 compose 命令的工程」跳过或标 transitioning(复用 `_project_states`/`get_command_execution_state`);hub 健康门在滚动期间排除「本任务正在重启的实例地址」,只对其余算 quorum。

### L3. 文档称 worker 端「namespace 不传不改也能跑」,实际 mode2 以 namespace 非空为触发前提,空则整段跳过同步
- **位置**:设计 §0.4/§2.1/§9 行257;`docker/nocobase/sync-plugins.js:353`(`else if (ADMIN_URL && NAMESPACE && SERVICE)`)、`:354`(URL 恒含 namespace)、`:386-388`(fall-through 跳过)
- **问题**:mode2 进入条件含 `&& NAMESPACE`,空时不成立→落到「未配置任何插件来源」→完全跳过同步(连 last-good 拉取都不走);且一旦进入 URL 永远拼 namespace,无法不传。「不改也能跑」为假。
- **建议**:§9 行257 改为「namespace 不传需改 mode2 进入 gate(去 `&& NAMESPACE`)+ 拼 URL 按需省略,属必改非可选」;或更简单——配置里保留 namespace(agent 忽略即可),与 §2.1 措辞统一。

### L4. 文档 §5 称 worker→agent「监听 127.0.0.1/本机网段」,现 ThreadingHTTPServer 默认绑 0.0.0.0
- **位置**:设计 §5、§2.1;`service-agent/config.py:26`、`core/health_server.py:67`/`:22-25`(非 /health 一律 404,worker-facing 路由尚不存在)
- **问题**:与 H1 同根的文档↔代码一致性表述。若 P1 直接在该 server 上加 worker-facing 路由而沿用默认 `HEALTH_HOST=0.0.0.0`,§2.4「同主机信任域」边界会比文档设想宽。
- **建议**:P1 落地 /plugins、/download 时用独立 `PLUGIN_SERVE_PORT` 且默认绑 127.0.0.1,勿复用默认 0.0.0.0 的 health server;§5 注明现 health server 默认 0.0.0.0,直接复用需收窄监听面。(与 H1 合并实施)

### L5. 原型:`go()` 切页只清空搜索框值,不还原 `filterPage` 隐藏的行 → 行「消失」
- **位置**:原型 line 543(只清 `gs.value`)、line 623(改 display 无复位入口)
- **问题**:先过滤过的页面切走再回来,搜索框已空但表格仍只显示之前匹配的行,其余行永久隐藏。
- **建议**:`go()` 清空 `gs.value` 后立即调 `filterPage('')`(空串把所有行设回 `display=''`),或加 `resetFilter(pg)` 在切页时调用。

### L6. 原型:服务详情点 rolltest 实例 → `openNode` 找不到键,抽屉直接关掉什么都不弹
- **位置**:原型 line 699(`openNode('rolltest-node1')`)、line 625-630(NODES 仅 wms-test-admin/admin2)、line 631-632(`if(!n)return;`)
- **问题**:`openService('rolltest')` 给每行挂 `closeInfoDrawer();openNode('rolltest-node1')`,但 NODES 没有 rolltest-node1/2/3,`openNode` 对未知键静默 return → 用户看到「点一下实例,抽屉直接消失,什么都没打开」。凡 NODES 未覆盖的实例都坏。
- **建议**:给 NODES 补 rolltest-node1/2(及 wms-prod 实例);或 `openNode` 在 `!n` 时 toast 提示,并把 `closeInfoDrawer()` 移到确认有数据之后。

### L7. 原型:infoDrawer 内触发 askConfirm 点「取消」会移除共享 mask,抽屉变无遮罩、点空白关不掉
- **位置**:原型 line 653/654/656/667/752/756
- **问题**:所有抽屉/弹窗共用同一 `#mask`。从已打开 infoDrawer 内部触发 confirmModal(版本历史「设为 active」、改版本)再点「取消」,`closeConfirm` 去掉 mask 但 infoDrawer 仍 `.on` → 抽屉悬空、背后无遮罩,且 mask onclick 失效,点空白无法关(只能靠 × 或 Esc)。
- **建议**:改成「只要还有任一弹层 `.on` 就保留 mask」——每个 close*/open* 后跑统一 `syncMask()`;或 askConfirm 从抽屉内触发时不重复操作 mask。

### L8. 原型:概览 `.grid2` 无 `display:grid` 基础规则,「最近操作/最近拉包」桌面端不并排
- **位置**:原型 line 219;line 157(唯一一条 `.grid2{grid-template-columns:1fr}` 还在媒体查询内);全文件 `display:grid` 0 命中
- **问题**:`.grid2` 桌面端退化为普通 block,两子 div 各 100% 宽上下堆叠,非设计的并排两栏。
- **建议**:加 `.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}`(媒体查询里已有的 1fr 覆盖会让窄屏自动塌成一列)。

### L9. 原型:镜像「发布新镜像」输入未转义就拼进 innerHTML/onclick,含引号/尖括号会打断「回滚到此」
- **位置**:原型 line 779(原样取值)、line 775(未转义双重拼接进 onclick)
- **问题**:`i[0]` 未做 HTML/属性转义,既作文本又拼进 `rollbackImage('...')`;镜像地址含单/双引号或 `<` 会破坏 onclick 字符串或注入标记。
- **建议**:渲染前对 `i[0]` 转义(文本侧 `escapeHtml`,属性侧改 `data-img` + 事件委托或 `encodeURIComponent`),不要把自由文本直接拼进 onclick。

### L10. 原型:服务页过滤会连未纳管面板的行一起隐藏,「去纳管」可能落到看着空的 tab
- **位置**:原型 line 623(跨 `#svc-managed`/`#svc-inbox` 两面板查 tbody)、line 569-574(`svcTab` 只切面板 display 不动行)、line 214(`go('services');svcTab('inbox')`)
- **问题**:在「已纳管」tab 过滤时 `#svc-inbox` 的行也被隐藏;`svcTab` 切 tab 不复位行也不重跑过滤,概览「去纳管」走过去时那两行可能仍被旧过滤隐藏 → 用户以为没有待纳管服务。
- **建议**:修复 L5(切页/切 tab 时 `filterPage('')` 复位)即可连带解决;另可让 `svcTab` 切换后对新面板重新应用当前过滤词。

### L11. 「serviceCode/nacosServiceName」「节点/实例/DiscoveredNode」「namespace 入参」三组术语在文中混用,边界不清
- **位置**:设计 §3.3 行122-124 / §8 行221(节点=DiscoveredNode)、§4.2 行182/184/185(UI 名「实例」、侧栏又「节点」)、§4.1 行150-161(实例=nacos 服务实例);§2.1 行56/§2.2 行69/§9 行248/257(/plugins 重名 + namespace 三处说法)
- **问题**:数据模型层叫「节点(DiscoveredNode)」、UI 层叫「实例」、侧栏又回「节点」,而 §4.1「实例」指 nacos 服务实例;一个 DiscoveredNode 与一个 nacos 实例是否恒 1:1 未界定。同时 worker→agent 与 agent→platform 用同名 `/plugins`,namespace 是否传/校验/忽略三处出入。
- **建议**:文首加术语表:DiscoveredNode = 一个被管 compose 容器(UI 统一叫「实例」,侧栏也改);nacos 实例 = 服务注册视角,正常与 DiscoveredNode 1:1(若可能不 1:1 须显式说明)。给两个端点不同路径(对内 `/agent/plugins`,回源 `/api/distribution/plugins`),namespace 语义定死一处。运行时文案(发布摘要、改版本确认、Agent 详情 line 603/606/615/650/682/750/756)全局把面向用户的「节点」替换为「实例」。

### L12. 原型:概览「最近操作/最近拉包」等暗色下硬编码浅色背景未覆盖,出现亮块
- **位置**:原型 line 605(内联 `background:#fafafa` 无暗色覆盖)、CSS line 109(`.todo .ti{background:#fafafa}` 在 `body.dark` 段未重定义)
- **问题**:发布摘要卡片、待处理图标底色等写死 `#fafafa`,暗色下变亮块。
- **建议**:写死的 `#fafafa/#f6f8fa` 改用变量(`var(--bg)` 或新增 `--soft`),`body.dark` 段补对应背景覆盖;约定「浅灰底块只用变量,不写十六进制」。

### L13. 原型:实例「更新」写死「优雅滚动 拉最新镜像+插件」,未按设计「按 diff 自动选 restart/pull-redeploy」;新建命名空间提示只返回 agentKey 漏 pull-token;顶栏搜索 0 命中无空态
- **位置**:原型 line 649(`nodeAct('更新')` 文案固定、无 diff)、line 281(新建命名空间仅 agentKey)、line 195/620-624/543(搜索按页过滤、无 0 命中提示);设计 §4.2 行185、§3.3 行126/§5 行198
- **问题**:三处小不对齐合并——① 单实例「更新」未计算该实例 vs 服务意图的 diff,也不区分 restart/pull-redeploy,丢了设计「自动选机制」要点;② pull-token 才是分发链凭据、agentKey 是控制链凭据,两者都在创建时下发,原型只提 agentKey 易让运维以为 pull-token 另配;③ 顶栏搜索 0 命中表格直接空白无「无匹配」提示。
- **建议**:单实例「更新」复用 `svcDiff`(该实例 vs 服务意图)→确认弹窗同样展示「将执行 restart 还是 pull-redeploy + 变更摘要」;新建命名空间提示改为「show-once 返回 agentKey(控制链)+ pull-token(分发链)」;搜索 0 命中时 tbody 末尾插「无匹配结果」占位,无表格页把过滤框置灰。

---

## 完整性遗漏(critic)

> 以下为 critic 维度发现的「方案该覆盖却没覆盖」的结构性缺口,与上面具体问题正交。

### G1.(High)P2「worker 侧只改配置」依赖的 sync-plugins.js 不在本仓库,文档未给跨仓交付/版本对齐路径
- **位置**:设计 §9 行232-243、§12 行288、§10 行265
- **问题**:已核实——`docker/nocobase/sync-plugins.js` 在 cnp 仓库,而设计文档与 service-agent/service-platform 在另一个 services-monorepo(本次 Glob 验证:本 checkout 只有 sync-plugins.js,无设计文档、无 Python 服务)。§9 所有「现有客户端已具备」能力(apiPath 可配、token 可选、last-good、跟随 301/302/307/308、version 优先)都是对另一仓当前实现的断言,本方案无法验证也无法随版本一起交付。cnp 侧脚本一旦与假设漂移,P2 真机静默失败,而平台侧 e2e 测不到 cnp 镜像里的脚本。
- **建议**:P0 把 worker↔agent 契约固化成可被两仓共同引用的 **contract test**——关键行为(重定向跟随、version 优先、tokenless、last-good)写成断言,放进本仓 agent e2e,用最小 fake-worker(复用那份脚本的 npm 包或 git submodule pin 到具体 commit)跑,而非口头断言;文档显式标注 cnp 侧脚本 pin 版本/commit 与负责人,改 agent 契约时两仓同 PR。

### G2.(High)两份 /plugins 契约对 namespace/service 入参相互打架,照文档实现会写出不一致代码
- **位置**:设计 §2.1 行56/58、§9 行248、§2.2 行69
- **问题**:同一份方案对 /plugins 入参给了三种互斥说法:worker→agent「namespace 不传」vs §9 样例又写 namespace 且「校验/忽略即可」vs agent→platform「必带 namespace」。「校验 vs 忽略」在安全上不同义——忽略则该参数是死字段(误导可跨 ns),校验则 tokenless worker 可传任意 ns 试探(与 §2.4 边界冲突)。实现者按不同段落会写出行为不同的 agent。
- **建议**:P0 钉死一种——推荐 worker→agent **完全不接受 namespace 入参**(agent 硬编码本 ns,任何传入一律拒绝而非忽略),删掉 §9 样例 namespace 字段;agent→platform 的 namespace 来源明确为「agent 配置的本 ns」而非 worker 透传。一句话:worker 侧零 ns 概念。(与 M5、L11 收口同向)

### G3.(Medium)`startDeploy` 对有真实实例的服务也可能用伪造 svc-1/svc-2,wms-prod(4 实例/0 在跑)会投到不存在的节点
- **位置**:原型 line 611-612(`inst` 为空 fallback `[svc+'-1', svc+'-2']`)、line 693、line 700(详情「发布」无守卫)
- **问题**:与 M14 同源但更进一层——fallback 分支的存在意味着任何 `inst` 数据缺失都会静默编造 2 个节点而非报错;wms-prod 发布会凭空滚动两个不存在的实例并 commit「发布完成」。
- **建议**:`openDeploy/startDeploy` 入口统一守卫:无在跑实例或对账=纳管未起/agent 离线时,弹窗只显示「无可投放实例」并禁用确认;删 fallback,实例为空就报错;详情抽屉与列表共用 `canDeploy(svc)`。

### G4.(Medium)全局只有单命名空间死文本、无切换器,3 个 namespace 的台账实为单 ns 视图,§3 多 agent 多 ns 模型 UI 上不可达
- **位置**:原型 line 197(死文本「命名空间:tamagawa-test」)、line 29(切换器列为「后续按需」)、line 207-210 vs 245-247(概览统计跨 ns 汇总 2/3、5 vs 实例页 1、2 对不上)、line 670-674(AGENTS 含 3 ns)
- **问题**:顶栏宣称当前 ns=tamagawa-test,概览四卡却是跨 ns 汇总,口径自相矛盾;实例页只列 tamagawa-test 的 2 容器,rolltest-agent 的 3 节点、k8s-prod-a 的 4 节点根本看不到。§3.3「节点/服务分表、全自动 upsert」的核心承载页只呈现一个 ns 子集,**方案主打的多机形态在演示里缺席**。
- **建议**:顶栏改真实切换器(切 ns → 过滤所有页表格与统计卡),明确每页统计口径(当前 ns 还是全局)使其与概览一致;实例页补齐 rolltest-agent/k8s-prod-a 的发现节点(agent 离线也要按 §3.1「docker 含 stopped 为主」展示)。

### G5.(Medium)P4「零中断」验收缺失败回滚定义,§8 无「发布进度/已成功实例集」字段,原型进度视图必然全成功
- **位置**:设计 §4.1 行162 / §4 行143-146(「失败即停」)、§6 行203-210(只谈 worker 启动/缓存,无投放中途失败)、§8 行218-228(无发布进度字段);原型 line 616-617、line 447/217(真实 drain 502 先例)
- **问题**:全文没定义「失败即停」之后的状态收敛——已 down/up 到新版的前 k 个实例 vs 停在旧版的后续,集群处于半迁移态,是回滚已动实例还是冻结等人工?§8 也没有记录「本次发布进度/已成功实例集」的字段,失败后无从知道滚到第几个。原型 `runNode` 纯定时器无失败分支,而审计页恰有真实 drain 502 说明这种半截失败会发生。
- **建议**:P0/P4 补「投放事务语义」——定义失败即停后两种模式(冻结半迁移态等人工 / 自动回滚已动实例到上一 deployed 版本);§8 增「发布运行记录」表(目标版本、实例序列、各实例状态、失败点)支撑断点续投与回滚;原型进度视图加失败路径(某实例 drain 502 → 标红 → 「已停在第 N 个,前 N-1 个已新版/已回滚」+ 重试/回滚按钮),让核心不变式可被 UI 验收。

### G6.(Low)agentKey 是 UI 一等操作,但数据模型/凭据章节只定义了 pull-token 的 provision 与轮换,agentKey 全程无归属表与轮换端点
- **位置**:原型 line 288/290/292(每行均有 `rotateToken('agentKey')`)、line 193(安全表把 agentKey 列为控制链凭据);设计 §3.3 行126、§5 行198(provision/轮换只写 pull-token)、§8 行218-228(无 agentKey 存储/轮换)
- **问题**:agentKey 在哪生成、存哪、怎么校验(§5 只含糊「hub 校验」)、轮换走哪条链全无定义,而 UI 已给按钮。会让实现者漏做或各自发明。与 L13「新建命名空间提示只返回 agentKey 未提 pull-token」恰是反向问题,二者都说明 agentKey/pull-token 的 provision 叙事两头不对齐。
- **建议**:§5/§8 补 agentKey 完整生命周期——provision(随 namespace 创建一并 show-once 返回 agentKey 与 pull-token)、存储(hub 侧存 agentKey hash + 绑 agentId)、校验位置、rotate-agentKey 端点与下发链;据此让原型新建命名空间提示同时返回二者。

---

## 总体评价与下一步建议

**值得肯定**:desired-state apply、自动发现消除手配、同主机一次回源喂多容器、last-good 不白屏——方向都对,且 §4.1 对「跨机滚动当前会失败」的现状诊断准确、修法方向正确。原型作为评审载体信息密度高,迭代日志透明。

**最该先解决的两类问题**:

1. **安全不变式必须前置(H1/H2 + M5/M6/M7/M9)**。当前安全模型建立在两个未被代码强制的前提(127.0.0.1 监听、namespace 1:1)上,默认配置下两条 RCE 级路径(0.0.0.0 tokenless 下载、无 sha256 校验 + 任意镜像 pull)实际打开。**这些不是 P5 增强,是上生产的门槛**,应在 P0/P1 钉成不变式并加验收用例。

2. **文档自洽性与「已有 vs 待建」的诚实标注(H3/H4/H5 + M10/M1)**。§12 现状对照表把多个零/半实现能力标「✅ 已有」,会让 P2/P3/P4 系统性低估;与 06-18/06-20 两份定稿的字段语义级冲突(serviceCode=nacosServiceName、dir 权威源)必须先消解,否则实现方无所适从。

**各维度成色**:
- **设计-架构**:骨架成立,闭环缺口集中在「自动触发」「跨 agent 协调」「失败回滚」三处衔接(均可补)。
- **设计-安全**:**最弱维度**,有真问题且严重,需专项收口。
- **设计-代码一致性 / 内部一致性**:有定位错误(M10)和多处术语/契约漂移,属可快速修正但必须修(照错文档改会改错文件)。
- **原型-JS 正确性**:多枚真实 bug,但都是局部可修;**无系统性架构错误**。
- **原型-UX / 设计对齐**:核心闭环 UI 断裂(H6)+ 多机视图缺席(G4)+ 失败态/兜底态全缺,**作为「验收设计不变式」的载体目前不合格**——它演示的全是理想路径,恰好掩盖了设计最看重的「失败即停 / last-good / 镜像漂移」。

**建议的推进顺序**:

1. **P0 契约 + 安全定稿(阻塞后续)**:① 钉死 worker↔agent /plugins 契约(G2/M5,worker 零 ns)并落 contract test(G1);② 把 sha256(H2)、127.0.0.1 监听(H1)、镜像 allowlist 非空(M6)、发现数据不驱动写寻址(H4/M7)写成安全不变式;③ 消解 serviceCode/nacosServiceName(H5)与 dir 权威源(H4)两处文档冲突;④ 补失败回滚事务语义 + 发布进度表(G5)。
2. **修正 §12 现状对照表**,把 docker stopped 采集(H3)、publish→滚动(M1)、跨 agent 协调(M10/L1)从「✅ 已有」改为「待建」,重排 P2/P3/P4 工时。
3. **原型对齐设计不变式**:补真实 diff 闭环(H6)、统一 `canDeploy` 守卫(M14/G3)、失败/兜底/stale 态(M13 + last-good/stale 信号)、多 ns 切换器(G4)、镜像漂移态(M16);其余 JS bug(L5-L10)与术语/暗色(L11/L12)批量清理。
4. **原型 JS bug 与文案**为最后一批,不阻塞设计评审通过。

**结论**:**方案不予直接放行,退回补 P0(契约 + 安全 + 文档冲突消解 + 失败回滚语义)后再评审**;原型需补齐设计不变式的可视化后才能作为验收基线。架构主体无需推倒重来。