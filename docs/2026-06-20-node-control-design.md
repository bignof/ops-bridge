# 节点控制（带审计的 compose 远程运维控制台）设计 v2

> 2026-06-20。v2 推翻 v1：v1 建立在「平台旧基线(无 rolling) + 自编 composeDir 优雅链」的假前提上(经 ultracode 评审 wx2kgaqis 推翻)。v2 **以统一基线 origin/main(含诉求1/2 rolling + P1a + P1-SPA)的真实能力为准** 重写。本特性分支 `feat/node-control` 已重建于 main。

## 1. 定位（北极星，不变）

给 **docker-compose 部署、无 k8s** 的机群做一个**带审计、看得见全机群、人点才动**的 compose 远程运维控制台：把运维本来 SSH 上节点敲的 `docker compose` 手动操作搬进安全可审计的 UI。**不是编排器**——不自愈、不调度、不扩缩容。

## 2. 非目标（守界，挡 k8s 化）

❌ 自愈/自动重启 ❌ 调度/扩缩容/副本管理 ❌ 资源指标采集(CPU/内存/磁盘) ❌ 插件状态(跨进 worker 域)。凡「自动」编排决策不碰。

## 3. 基线澄清（v2 的根本修正）

origin/main 实测已具备(v1 误判为缺失):
- **hub dispatch 已鉴权**：`service-hub/app/routers/commands.py` 的 `dispatch_command`/`retry_command` 首行 `_require_admin_token(X-Admin-Token)`。平台 BFF 用 `HUB_ADMIN_TOKEN`(服务端)调用即合法,匿名调用 403。
- **优雅 rolling 真实存在**：`POST /api/rolling-restart {agentId, serviceName, force}` → `_run_rolling`：agent `list-instances`(走 **nacos** `list_healthy_instances`)→ 过滤 healthy → **要求 ≥2 健康**(否则非 force 拒、force 则 degraded)→ 按 **containerId** 逐个重启(带 `shutdownTimeoutSec` drain)→ fail-stop(失败标剩余 skipped)→ 任务跟踪 `GET /api/rolling-restart/{id}` + `acknowledge` + 启动扫描中断恢复。
- **agent 能力**：`core/handlers.py` 按 **projectDir** 管多个 compose 项目(`_project_locks` 按项目串行、list-projects);`core/rolling.py` 处理 list-instances + 按 containerId 重启;`services/nacos_client.py` 列实例。
- **agent:服务 = 1:N**：一台 agent(宿主机)管多个 compose 项目;命令按 projectDir 寻址。

## 4. 架构与数据流

```
浏览器(单 admin) ─JWT─▶ service-platform(BFF)
                          │ HUB_ADMIN_TOKEN(服务端) + X-Admin-Token
                          ▼
                        service-hub ──ws dispatch──▶ service-agent(各宿主机)
                          │ 已有: dispatch(鉴权)/CommandModel(审计)/rolling 编排/list_agents
                                                      │ docker compose(按 projectDir) / nacos 列实例 / 调本机 worker drain
                                                      ▼
                                          节点上 N 个 compose 项目(NocoBase worker 等)
```

平台不直连 agent;在线态读 hub 实时(AgentModel.status/remote_addr,hub 单实例进程内 ws 表为真值)。

## 5. 节点对象模型：(agent × service)

节点页的**行 = (agent, service)**,不是 agent 单值(纠正 v1):
- **agent 维**(host 级,来自 hub `AgentModel`):agentId、remote_addr(ip)、status(online/offline)、last_seen_at。
- **service 维**(来自两路):① agent `list-projects` 回该 agent 的 compose 项目(serviceName/projectDir/运行态/imageTag);② `list-instances`(nacos)回该 service 的健康实例(containerId/healthy)——提供**健康信号**(addressing 评审「只有 online/offline」缺口)。
- 平台 BFF 把 agent 列表与各 agent 的 projects/instances 组合成 (agent×service) 列表;**serviceName↔projectDir 映射在 agent 侧**(平台/hub 只传 serviceName 或 projectId,不传宿主机绝对路径,避开主机/容器路径错配)。

> reported 自描述若要持久化(imageTag 等),`AgentModel` 加 `reported` JSON 列**须配 Alembic 迁移**(hub 用迁移非 auto-sync);但优先**按需现查**(list-projects/list-instances 实时),能不落库就不落,避免过期(评审「reported 过期无新鲜度」缺口)。

## 6. 操作语义（grounded 在真实能力）

| 操作 | 实现 | 优雅 | force |
| --- | --- | --- | --- |
| **重启** | 优雅=复用 `/api/rolling-restart`(实例级/containerId/≥2健康/drain/fail-stop);force=agent compose restart(按 projectDir) | ✅ 复用现成 | compose restart |
| **拉镜像重部署** | 优雅=rolling-restart 语义的「先 drain 再 pull+up」(扩 rolling 或 agent 新动作);force=复用现有 `handle_update`(compose pull+up by projectDir) | 扩展 | 复用 update |
| **停止/下线** | agent 新动作:优雅=先调本机 worker `/api/k8s/shutdown` drain + nacos 注销 → compose stop(按 projectDir);force=compose stop/down 直停 | 新增 | 新增 |
| **启动** | agent 新动作:compose up -d(按该 agent 已知 projectDir;停机服务无运行实例,故按 projectDir 而非 list-instances) | 无优雅之分 | — |

- **start/stop 是真·新增** agent 动作(rolling 只重启);按 **agent 自己的 projectDir** 寻址(list-projects 已知),不依赖运行实例、不传宿主机路径。
- **pull-redeploy 与现有 `handle_update` 澄清**:force 重部署直接复用 update(避免重复造轮子);优雅重部署=drain 后 update。
- 幂等:start 已在跑→no-op 成功;stop 已停→no-op 成功;对无该 project 的 agent→明确报错。

## 7. 安全

- **dispatch 已 token-gated**(X-Admin-Token);平台 BFF 服务端持 `HUB_ADMIN_TOKEN`,不下发前端;默认拒绝中间件(P1a)守 `/api/**`。
- **审计**:复用 hub `CommandModel`(action/target/status/output/error)+ rolling 任务;**`requested_by` 由平台服务端按已认证 admin 身份填**(不取客户端可伪造值);平台「操作审计」页展示。
- **二次确认**(输 serviceName/节点核对)为前端防误点;**服务端真闸是 token + 默认拒绝**(不依赖前端确认)。
- **force stop/down 护栏**:一次一个目标、明确高危标识;不提供批量。
- **pull-redeploy 镜像来源**:imageTag 仅允许平台台账(plugin/service 配置)登记过的,不接受任意镜像串(防供应链)。
- **P1-deploy-isolation 同期**(仍需,针对**读端点 + 网络**):给 hub 的零鉴权**只读**端点(list_agents 等)补 `_require_admin_token`;hub compose 绑内网/internal network + 异网段直连对抗脚本。(dispatch 写端点已 gated,但读端点 + 网段仍要补,纵深。)
- agent→本机 worker `/api/k8s/shutdown`:仅 agent 调本机(localhost/同 compose 网络);该端点鉴权与可达性在 P1-deploy-isolation 一并核(评审「/api/k8s/shutdown 零鉴权」)。

## 8. 失败模式 / 并发（吸收评审）

- **优雅 drain 超时/卡死**:复用 rolling 的 `rolling_shutdown_timeout`/`rolling_ready_timeout`;超时→该实例失败→rolling fail-stop 标剩余 skipped(不自动转 force,人工决定)。start/stop 也设超时,超时报失败入审计。
- **操作进行中 + 结果反馈**:restart 走 rolling 任务态(pending/running/done/degraded/failed/skipped),前端轮询 `GET /api/rolling-restart/{id}` 展示;start/stop 走 CommandModel status/output。**点完看得到成败**(addressing 评审「只留痕不知成败」)。
- **agent 中途断连**:沿用 hub 现有命令超时/中断恢复(rolling 有启动扫描中断任务);start/stop 命令设超时,超时标 failed 不悬空。
- **同节点并发**:agent 按 projectDir 串行锁(已有);hub/平台层对同 (agent,service) 的进行中操作加去重(拒第二个并发同目标操作)。
- **生命周期不变式**:agent 是**独立容器/进程,不在被控 compose 项目内**——stop/重启被控服务不影响 agent ws 连接,故 stop 后仍能 start(addressing v1 悖论)。spec 明列此不变式 + 护栏:**禁止把 agent 自身所在 project 列为可操作目标**。

## 9. 组件改动清单

- **service-agent**:新增 `start`/`stop`(优雅+force)compose 动作(按 projectDir,调本机 worker drain 的封装);`pull-redeploy` 复用/扩 `handle_update`;确保 `list-projects` 回 serviceName/imageTag/运行态。(list-instances/rolling 已有。)
- **service-hub**:dispatch 放行新 action(已 gated);如需 reported 落库则加列 + Alembic 迁移;node 列表聚合端点(或平台 BFF 直接组合 list_agents + 转发 list-projects/list-instances)。
- **service-platform(BFF)**:`GET /api/nodes`(agent×service 聚合)、`POST /api/nodes/{agentId}/{serviceName}/{action}`(start/stop/restart/pull-redeploy + mode,requested_by 服务端填)、`GET /api/node-operations`(审计,代理 CommandModel + rolling 任务)。复用 hub_client + HUB_ADMIN_TOKEN。
- **service-platform/web(SPA)**:「节点」页(agent×service 表 + 行操作 + 二次确认 + 操作进行态/结果)、「操作审计」页。复用 P1-SPA 既有 CrudTable/ShowOnce/api client 范式。
- **P1-deploy-isolation**(同期,独立计划):hub 只读端点补 token + 网段隔离 + 对抗脚本。

## 10. 验收

- 节点页列出 (agent×service),在线态/健康(nacos)与实时一致。
- 重启(优雅)复用 rolling 真零中断(≥2 健康时在途不断);force 各操作如期。
- start/stop 幂等;stop 后可再 start(验证 agent 独立性不变式);禁操作 agent 自身 project。
- 每操作留审计(requested_by=服务端 admin、action/mode/目标/结果),点完看得到成败。
- 镜像来源校验:非台账登记 imageTag 被拒。
- P1-deploy-isolation:hub 只读端点补 token 后匿名 403;异网段直连 hub 被拒。
- 砍掉项(资源/插件状态/自愈/调度)确无实现。

## 11. 范围 / 依赖 / 分期

- **基线 = origin/main**(rolling + P1a + SPA 统一);本特性分支 off main。
- **交付**:① `节点控制` 实现计划(agent start/stop + BFF + SPA 节点页/审计页;重启/重部署优雅复用 rolling);② `P1-deploy-isolation`(已有计划,需补 dispatch 外的只读端点鉴权范围)同期。
- **MySQL lane / 既定除外**:沿用 P1a。
- **明确不做**:资源指标、插件状态、自愈、调度。
