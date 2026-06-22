# 节点控制（带审计的 compose 远程运维控制台）设计 v3

> 2026-06-20。v3 修正 v2 经 ultracode 评审(wvb3jiw7n,确认 21)暴露的核心错误:**v2 又把不存在的 agent `list-projects` 当「已有能力」**,致 (agent×service) 模型/ start 寻址/ imageTag 悬空。v3 的根本修法:**以平台 `Service` 表为 (agent×service) 权威源**,并**诚实列出 agent 需新增的全部能力**(不再把新增当已有)。基线=origin/main(rolling+P1a+SPA 统一)。

## 1. 定位（不变）

docker-compose 机群(无 k8s)的**带审计、看得见全机群、人点才动**的 compose 远程运维控制台。把运维本来 SSH 上节点敲的 `docker compose` 手动操作搬进安全可审计 UI。**不是编排器**——不自愈/不调度/不扩缩容。

## 2. 非目标

❌ 自愈/自动重启 ❌ 调度/扩缩容/副本 ❌ 资源指标采集 ❌ 插件状态。

## 3. 权威数据源 = 平台 `Service` 表（v3 核心修正）

节点页**行 = `Service` 表每行**(不靠 agent list-projects):`service-platform/app/db_models.py Service` 已有 `namespace_id(=agentId/host) / service_code / dir(compose 目录) / default_image / nacos_service_name`——四件套齐。每行组合三态:
- **在线态**:`namespace_id → agentId →` hub `AgentModel.status/remote_addr/last_seen`(hub 进程内实时,不缓存)。
- **健康信号**:`nacos_service_name →` agent `list-instances`(走 nacos,**已有**)→ healthy 实例数/containerId。
- **运行态/实际镜像**(可选):见 §7 imageTag。
- **compose 寻址**:`dir`——**权威路径来自台账,且须是 agent 容器内可见路径**(部署约定:Service.dir 按 agent 视角填,闭合 v1 主机/容器坐标系错配)。

> 「停机服务可见性」由此自然解决:Service 表登记的服务即便停了也在表里 → 节点页可见 → 可点 start(v2 的 start 悖论消解,不依赖 list-projects 列运行容器)。

## 4. agent 需新增能力（诚实清单，全部为新增，非「已有」）

main 的 agent 现有:`update`/`restart` handler、`list-instances`(nacos)、`graceful-restart`(rolling 用,含 `_validate_health_base_url` SSRF 硬化 + 调本机 worker `/api/k8s/shutdown`)。**以下均需新增**:
1. `start`(compose up -d，按 dir)/`stop`(优雅:复用 `_validate_health_base_url`→调本机 worker drain→compose stop;force:compose stop/down)/`force-restart`(compose restart)/`pull-redeploy`(优雅:drain→pull→up;force:复用 `update`)。
2. **dir 安全校验(闭合 v1+v2 硬伤)**:`_validate_base` 增 `realpath(dir)` 必须在 `MANAGED_PROJECTS_ROOT` 之下(防 `..` 穿越/任意目录);**拒绝命中 agent 自身 project**(agent 配 `SELF_PROJECT_DIR` 或据自身容器 compose project label 自识别)。agent 挂宿主 docker.sock,此校验是防自杀+防宿主越权的最终闸。
3. **镜像来源白名单**:pull 前校验 image 的 registry 前缀∈agent 配置白名单(env),非法即 failed——把「防供应链」下沉到不可绕过的执行点(平台台账校验仅 UX 层)。
4. **(可选) list-projects**:扫 `MANAGED_PROJECTS_ROOT` 子目录 compose 文件 + `compose ps` 取运行态——仅用于「发现 Service 表未登记的野 project」;**非节点页主数据源**(主数据源是 Service 表)。
5. **(可选) 实际 imageTag**:对 matched 容器 `docker inspect` 取 Image 上报。
6. **能力/版本上报**(register 帧):供节点页对旧 agent 禁用其不支持的操作 + 友好提示(滚动升级期兼容)。

## 5. 寻址与两套路径对齐（闭合 v2 Critical）

- **compose 操作**(start/stop/force-restart/pull-redeploy)按 **Service.dir**(台账权威,agent 校验在 root 内)。
- **优雅 restart** 复用 main `/api/rolling-restart`(nacos 列实例 → 按 containerId 逐个、≥2 健康、drain、fail-stop、任务态)。
- **两路径对齐(必做)**:`match_instance` 现按 端口/IP 映射 nacos 实例→容器,**不知 compose project**。v3 要求 match_instance **额外回传容器的 `com.docker.compose.project` label,与 Service.dir 推得的 project 比对,不一致则标 `matched=false`、拒绝该实例优雅操作**;并明示**粒度差异**:优雅=实例级(单 containerId)、force=目录级(`compose` 作用该 dir 全部容器)——多实例同目录服务 force stop 会一次停光,UI 须强警示。验收加「优雅与 force 作用于同一组容器」对账。

## 6. 操作 mode 承载（闭合 v1 C4）

dispatch 帧 `{type,requestId,action,dir,image}` 与 `CommandModel` 无 mode 位。v3:**给 dispatch 帧 + CommandModel 增 `mode`(graceful/force)字段 + 配 Alembic 迁移**(hub 用迁移非 auto-sync);现有 `action: Literal["update","restart"]` 放宽纳入新 action(**优先放宽 Literal、不新增 hub 写端点**,鉴权天然继承)。

## 7. imageTag 两义务区分

- **期望镜像**(节点页展示 + pull-redeploy 校验基准):平台 `Service.default_image`(台账,**已有**)。
- **实际运行镜像**(可选展示):agent `docker inspect`(§4.5 新增);一期可不做,节点页先展示期望镜像。

## 8. 安全（评审重点，闸全部下沉到服务端/agent）

- **dispatch 已 token-gated**(`commands.py` 首行 `_require_admin_token`,实测属实);新 action 走放宽 Literal 不新增端点;**若必须新增 hub 写端点 → 首行必 `_require_admin_token` + 补「未鉴权写端点」回归测试**;建议把 hub 逐端点手动鉴权收敛为依赖/中间件,根除「漏写一行=匿名 RCE」(v1 S1 同类)。
- **requested_by 服务端派生**:hub 据 admin token 关联身份**强制覆盖** requested_by(X-Requested-By 仅作 hint),并记 caller 指纹(token 指纹/来源)旁证;不信任客户端自报。纳入 P1-deploy 写端点纵深。
- **force stop/down 服务端护栏**(非仅 UI):① 全局滑窗速率限制(N 分钟 force-down ≤ K 次,超限需显式解锁);② **不变式「不可停掉某 service 最后一个健康实例」**(force 也校验,类比 rolling ≥2);③ force-down step-up(重输凭据/二次解锁)。
- **镜像白名单**在 agent/hub 强制(§4.3),平台台账仅 UX。
- **/api/k8s/shutdown**:新 stop 的 drain **必复用 `_validate_health_base_url`**(防 SSRF;验收:传公网/域名 healthBaseUrl 被拒不发 shutdown);该端点**鉴权 owner 明确归 P1-deploy 新增 Task**(不再两文档踢皮球)。
- **禁操作 agent 自身 project**:落在 agent §4.2(realpath+self-project),非仅文字。

## 9. 失败模式 / 并发 / 幂等

- **drain 超时**:复用 rolling 的 `rolling_shutdown_timeout/ready_timeout`;超时→失败入审计,**不自动转 force**(人工决定)。
- **结果反馈**:restart 走 rolling 任务态(轮询 `/api/rolling-restart/{id}`);start/stop/redeploy 走 CommandModel status/output——**点完看得到成败**。
- **孤儿命令**:start/stop 设超时,超时标 failed 不悬空(沿用 hub 命令超时;rolling 有启动扫描中断恢复)。
- **并发去重**:对同 (agentId, service_code) 进行中操作加互斥(拒第二个)——**需在 hub 加进行中态键**(CommandModel 现无 in-progress 唯一键,v3 列为改动);agent 侧已有按 dir 串行锁兜底。
- **生命周期不变式**:agent 是独立容器/进程、不在被控 project 内 → stop/重启被控服务不断 agent ws;**§4.2 self-project 守卫是该不变式的强制点**。

## 10. BFF 节点页读路径降级（闭合 v2 Important）

节点页 = Service 表(静态,稳)+ 每 agent 的在线态(hub HTTP,快)+ 按需 list-instances(ws 往返,可能超时)。**降级契约**:per-agent 并发 fan-out + 独立超时 → 某 agent/nacos 超时只把该行标「健康不可用/degraded」**不阻塞整页**;agent 离线行用 Service 表静态信息 + 离线态渲染(不发 ws);nacos 失败与 agent 离线区分展示;可选短 TTL 缓存兜抖动。验收:单 agent 卡死不拖垮节点页。

## 11. 部署约束（显式）

- **hub 单实例**:在线态(进程内 _connections)、call_agent(进程内 _pending)、rolling 任务(内存 set)、中断恢复(重启标 interrupted、需人工 acknowledge 才释放 active_key)全在进程内。→ **hub 不支持多副本**(P1 可接受);hub 单点故障=控制台失能;hub 重启后进行中 rolling 需人工 ack 才恢复同目标操作。路线图:多副本须把 ws 路由/pending/任务态外置(redis/DB)。
- **P1-deploy-isolation 硬依赖(非「同期」模糊承诺)**:hub 只读端点(GET /api/agents、/api/commands*、list-instances 数据面、agent /health)补 token + hub 绑 internal network + /api/k8s/shutdown 鉴权,**必须先于或同 PR 随节点控制合入**;§13 验收把「hub 读端点匿名 403 + 异网段直连被拒」列为节点控制放行**阻塞项**。

## 12. 测试策略

- agent:monkeypatch `run_compose`/`docker_cli` 测 start/stop/force-restart 幂等(已跑/已停/无该 project)、dir 越界被拒、self-project 被拒、image 白名单、drain SSRF 校验(仿 `test_handlers.py`/`test_rolling.py`)。
- hub:mode 承载 + requested_by 服务端派生 + force 速率/最后健康实例不变式 + 新 action 鉴权回归(未鉴权写端点应红);rolling 复用回归(不破坏 `_run_rolling`)。
- platform BFF:mock hub_client 测 Service 表驱动的 (agent×service) 聚合 + 单 agent 超时降级 + 审计 requested_by。
- SPA:节点页/审计页 smoke + 二次确认 + 操作进行态/结果;mock 对齐后端真实 *Out(防假绿)。

## 13. 验收

- 节点页(Service 表驱动)列全机群 (agent×service),在线/健康实时;停机服务可见可 start。
- 启停重启重部署:优雅(rolling/drain)零中断、force 如期;优雅与 force 作用同一组容器(对账)。
- 安全:dir 越界/self-project/非白名单 image **被 agent 拒**;force-down 触发服务端速率/最后健康实例闸/step-up;requested_by 由 hub 派生不可伪造;hub 读端点匿名 403 + 异网段直连被拒(P1-deploy 阻塞项)。
- 审计:每操作留痕(who[hub 派生]/action/mode/目标/结果),点完看得到成败。
- 单 agent 卡死不拖垮节点页;hub 重启后 interrupted rolling 需 ack(运维已知)。
- 砍掉项(资源/插件状态/自愈/调度)确无实现。

## 14. 范围 / 依赖 / 分期

- 基线=origin/main;分支 off main。
- **交付**:① 节点控制(agent 6 项新增 + hub mode/requested_by/force 闸/聚合 + BFF + SPA);② **P1-deploy-isolation(扩范围:加 /api/k8s/shutdown 鉴权 + requested_by 派生 + 写端点纵深)必须先/同 PR 合入**。
- MySQL lane / 既定除外:沿用 P1a。
- 明确不做:资源指标、插件状态、自愈、调度。
- **诚实评估**:本特性比「薄控制台」重——agent 6 项新增 + 安全闸下沉 + P1-deploy 扩范围;非小改。
