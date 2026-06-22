# 节点控制（带审计的 compose 远程运维控制台）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans。Steps 用 checkbox (`- [ ]`)。

**Goal:** 给 docker-compose 机群(无 k8s)建一个带审计、看得见全机群、人点才动的 compose 远程运维控制台:平台 BFF→hub(已 token-gated dispatch)→agent(ws)→节点 compose;操作 启动/停止(优雅+force)/重启(优雅复用 rolling / force)/拉镜像重部署。

**Architecture:** 平台「节点」页以**平台 `Service` 表为权威源**(每行=(agent×service),dir/default_image/nacos_service_name 已有),组合 hub 实时在线态 + nacos 健康。compose 操作按 `Service.dir`(agent 校验在 `MANAGED_PROJECTS_ROOT` 内 + 拒自身 project);优雅重启复用 main 现成 `/api/rolling-restart`(nacos 实例级/containerId/drain);审计复用 hub `CommandModel`(requested_by 由 hub 派生)。

**Tech Stack:** service-agent(Python/websocket/docker compose)、service-hub(FastAPI/SQLAlchemy/alembic)、service-platform(FastAPI BFF + React/Vite/antd SPA)。基线 origin/main(rolling+P1a+P1-SPA 统一),分支 `feat/node-control` off main。

## Global Constraints

- **非目标(不做)**:自愈/自动重启、调度/扩缩容、资源指标采集、插件状态。凡「自动」编排不碰。
- **权威源=平台 `Service` 表**(`service-platform/app/db_models.py` Service:`namespace_id`(=agentId/host)、`service_code`、`dir`(compose 目录,agent 容器内可见路径)、`default_image`、`nacos_service_name`)。节点列表/寻址/期望镜像全取此,**不靠 agent list-projects**。
- **优雅复用 main `/api/rolling-restart`**(service-hub/app/routers/rolling.py:nacos list-instances→containerId 逐个→≥2 健康→drain→fail-stop→任务态),**不自建 composeDir 优雅链**。
- **compose 操作按 `Service.dir`**;agent `_validate_base` 强制 `realpath(dir)` 在 `MANAGED_PROJECTS_ROOT` 下 + 拒命中 `SELF_PROJECT_DIR`。
- **审计=hub `CommandModel`**;`requested_by` 由 hub 据 admin-token 身份**派生**(X-Requested-By 仅 hint)。
- **安全闸在服务端/agent**:force 服务端速率限制 + 「不可停某 service 最后一个健康实例」不变式 + step-up;image registry 白名单在 agent/hub 强制;dispatch 已 `_require_admin_token`,新 action 走放宽 `Literal` 不新增 hub 写端点(若新增必首行 `_require_admin_token` + 回归测试)。
- **mode(graceful/force)**:dispatch 帧 + CommandModel 加 `mode` 字段 + **Alembic 迁移**(hub 用迁移非 auto-sync)。
- **hub 单实例**:online/call_agent/rolling 任务全进程内,不支持多副本(P1 可接受);hub 重启进行中 rolling 需人工 acknowledge。
- **P1-deploy-isolation 硬前置**(独立计划 `2026-06-19-...-p1-deploy-isolation.md`,需扩范围:hub 只读端点补 token + 绑 internal network + /api/k8s/shutdown 鉴权 + requested_by 派生归此或本计划 H2):**必须先于或同 PR 随本特性合入**;「hub 读端点匿名 403 + 异网段直连被拒」是本特性放行阻塞项。
- 语言:产出全中文(代码注释/commit/PR);commit 前缀英文。提交 `feat(...): 中文`;分支 `feat/node-control`,勿 push。
- 测试:agent 用 pytest monkeypatch `run_compose`/`docker_cli`(仿 `service-agent/tests/test_handlers.py`);hub 用 pytest(仿 `test_rolling.py` FakeHub);platform 后端 pytest(`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`,沿用 P1a 覆盖率门 `--cov-fail-under=94`);SPA vitest(沿用覆盖率门)。mock 一律对齐后端真实 `*Out`(防假绿)。

## File Structure

- **service-agent**:`config.py`(+MANAGED_PROJECTS_ROOT/SELF_PROJECT_DIR/IMAGE_REGISTRY_ALLOWLIST)、`core/handlers.py`(+handle_start/stop/force_restart/pull_redeploy + `_validate_base` 守卫 + HANDLERS)、`core/graceful.py`(新:抽 drain 封装,复用 rolling 的 `_validate_health_base_url`)、`services/compose.py`(复用)、`core/ws_client.py`(register 能力帧)、`tests/`。
- **service-hub**:`app/models.py`(action Literal 放宽 + `mode`)、`app/db_models.py`(CommandModel.mode)、`migrations/versions/`(+mode 列)、`app/routers/commands.py`(requested_by 派生)、`app/routers/node_ops.py`(新:force 闸 + 聚合,或并入 commands)、`app/api_support.py`(`_derive_requested_by`)、`tests/`。
- **service-platform/app**:`hub_client.py`(+dispatch_node_action/list_instances/list_commands)、`routers/nodes.py`(新:/api/nodes、/api/nodes/{agentId}/{serviceCode}/{action}、/api/node-operations)、`models.py`(NodeOut/NodeOperationOut)、`main.py`(include router)、`tests/`。
- **service-platform/web**:`src/pages/NodesPage.tsx`、`src/pages/NodeOperationsPage.tsx`、`src/api/resources.ts`(+nodes/node-operations)、`src/layout/AppShell.tsx`(+运维组菜单)、`src/main.tsx`(lazy 路由)、`__tests__/`。

---

### Task 0（前置，本计划不展开）：P1-deploy-isolation 扩范围并先/同 PR 合入

**不在本计划实现**——见独立计划。必须落地:① hub 只读端点(`GET /api/agents`、`GET /api/commands*`、list-instances 数据面、agent `/health`)补 `_require_admin_token`;② hub 绑 internal network + 端口不对外;③ worker `/api/k8s/shutdown` 鉴权 owner 落实;④ 异网段直连 hub 对抗脚本。**验收门**:hub 读端点匿名 403、异网段直连被拒——未过不得放行节点控制上线。

---

### Task 1（agent）：dir 安全守卫 + config

**Files:** Modify `service-agent/config.py`、`service-agent/core/handlers.py`;Test `service-agent/tests/test_handlers.py`

**Interfaces:**
- Produces:`config.MANAGED_PROJECTS_ROOT`(str)、`config.SELF_PROJECT_DIR`(str|"");`_validate_base(ws,data)` 返回 `(request_id, action, project_dir)` 中 project_dir 已通过 realpath+root+self 校验。

- [ ] **Step 1: 失败测试**(`test_handlers.py`):
```python
def test_validate_base_rejects_dir_outside_root(monkeypatch, fake_ws):
    monkeypatch.setattr(config, "MANAGED_PROJECTS_ROOT", "/data")
    res = handlers._validate_base(fake_ws, {"requestId":"r1","action":"restart","dir":"/etc"})
    assert res is None and fake_ws.last_error_contains("不在受管目录")

def test_validate_base_rejects_self_project(monkeypatch, fake_ws, tmp_path):
    monkeypatch.setattr(config, "MANAGED_PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SELF_PROJECT_DIR", str(tmp_path/"agent"))
    (tmp_path/"agent").mkdir()
    res = handlers._validate_base(fake_ws, {"requestId":"r1","action":"stop","dir":str(tmp_path/"agent")})
    assert res is None and fake_ws.last_error_contains("禁止操作 agent 自身")
```
- [ ] **Step 2:** `pytest service-agent/tests/test_handlers.py -k validate_base -v` → FAIL。
- [ ] **Step 3: 实现**。`config.py` 加:
```python
MANAGED_PROJECTS_ROOT = os.getenv('MANAGED_PROJECTS_ROOT', '/data')
SELF_PROJECT_DIR      = os.getenv('SELF_PROJECT_DIR', '')   # agent 自身 compose 目录,禁止被操作
```
`handlers.py` 的 `_validate_base` 在 isdir 校验后加(import config):
```python
    real = os.path.realpath(project_dir)
    root = os.path.realpath(config.MANAGED_PROJECTS_ROOT)
    if os.path.commonpath([real, root]) != root:
        send_error(ws, request_id, f"dir 不在受管目录 {root} 内: {project_dir}")
        return None
    if config.SELF_PROJECT_DIR and os.path.commonpath([real, os.path.realpath(config.SELF_PROJECT_DIR)]) == os.path.realpath(config.SELF_PROJECT_DIR):
        send_error(ws, request_id, "禁止操作 agent 自身 project")
        return None
```
- [ ] **Step 4:** 测试通过 + 既有 test_handlers 不回归。
- [ ] **Step 5: Commit** `feat(agent): dir realpath 守卫 + 拒自身 project(节点控制安全闸)`

---

### Task 2（agent）：start / stop(force) / force-restart 处理器

**Files:** Modify `service-agent/core/handlers.py`;Test `tests/test_handlers.py`

**Interfaces:**
- Consumes:`run_compose(project_dir, args)->(ok,out)`、`_reply`、`find_compose_file`。
- Produces:HANDLERS 增 `start`/`stop`/`force-restart`(graceful stop 见 Task 3,本任务 stop 只做 force);三者签名 `(ws,data,request_id,project_dir)`。

- [ ] **Step 1: 失败测试**:mock `run_compose` 断 start→`['up','-d']`、stop(force)→`['stop']`、force-restart→`['restart']`;幂等:start 已在跑(run_compose 返回 ok)→success。
```python
def test_handle_start_runs_up(monkeypatch, fake_ws):
    calls=[]; monkeypatch.setattr(handlers,"run_compose",lambda d,a:(calls.append(a) or (True,"ok")))
    monkeypatch.setattr(handlers,"find_compose_file",lambda d:d+"/docker-compose.yml")
    handlers.handle_start(fake_ws,{},"r1","/data/svc")
    assert ['up','-d'] in calls and fake_ws.last_status()=='success'
```
- [ ] **Step 2:** FAIL(handler 未定义)。
- [ ] **Step 3: 实现**(仿 `handle_restart`,各自 find_compose_file + run_compose + `_reply`;mode=force 的 stop 用 `['stop']`,**不用 down**,避免删容器影响 start;HANDLERS 注册 `'start'/'stop'/'force-restart'`)。
- [ ] **Step 4:** 通过。 **Step 5: Commit** `feat(agent): start/stop(force)/force-restart compose 处理器`

---

### Task 3（agent）：优雅 stop（drain→stop）+ pull-redeploy

**Files:** Create `service-agent/core/graceful.py`;Modify `core/handlers.py`、`core/rolling.py`(抽 `_validate_health_base_url` 为公共);Test `tests/test_graceful.py`

**Interfaces:**
- Consumes:`rolling._validate_health_base_url(base)`(SSRF 守卫:仅 http/https+非公网 IP+拒域名)、`http_client.post`。
- Produces:`graceful.drain_then(ws, data, request_id, project_dir, after_action)`;handlers `handle_stop` 按 `data['mode']` 分流(graceful=drain→stop / force=Task2);`handle_pull_redeploy`(graceful=drain→update / force=复用 handle_update)。

- [ ] **Step 1: 失败测试**:① graceful stop 传公网/域名 healthBaseUrl → 被拒、**不发 shutdown、不 stop**;② 合法 healthBaseUrl → 先 POST `/api/k8s/shutdown` 再 `run_compose(['stop'])`(断调用次序)。
- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**。把 `core/rolling.py` 的 `_validate_health_base_url` 移到/复用于 `graceful.py`(或 import);`drain_then`:校验 base→`http_client.post(f"{base}/api/k8s/shutdown", json={"shutdownTimeoutSec":...}, allow_redirects=False)`→等待→执行 after_action(compose stop / update)。`handle_stop`/`handle_pull_redeploy` 读 `data.get('mode','graceful')` 分流。HANDLERS 注册 `'pull-redeploy'`。
- [ ] **Step 4:** 通过(含 SSRF 拒绝用例)。 **Step 5: Commit** `feat(agent): 优雅 stop(drain→stop)+ pull-redeploy(复用 SSRF 守卫)`

---

### Task 4（agent）：镜像 registry 白名单 + 能力上报

**Files:** Modify `config.py`(IMAGE_REGISTRY_ALLOWLIST)、`services/compose.py`(update_image_in_compose 前校验)或 `handlers.py`(pull 前)、`core/ws_client.py`(register 能力帧);Test `tests/test_compose.py`、`tests/test_ws_client.py`

**Interfaces:**
- Produces:pull/update/pull-redeploy 前若 image 的 registry 前缀 ∉ `IMAGE_REGISTRY_ALLOWLIST` → failed;agent 连接后发 `{type:'register', capabilities:[...支持的 action], agentVersion:...}`。

- [ ] **Step 1: 失败测试**:① image=`evil.com/x:1` 非白名单 → update/pull-redeploy failed(不执行 pull);② 白名单内放行;③ ws on_open 后发 register 帧含 capabilities。
- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**。`config.IMAGE_REGISTRY_ALLOWLIST = [x for x in os.getenv('IMAGE_REGISTRY_ALLOWLIST','').split(',') if x]`(空=不限,但 spec 要求生产配置);在镜像变更前校验前缀;`ws_client._on_open` 发 register 帧(capabilities=list(HANDLERS) + agentVersion)。
- [ ] **Step 4:** 通过。 **Step 5: Commit** `feat(agent): 镜像 registry 白名单 + 能力/版本上报帧`

---

### Task 5（hub）：mode 字段 + action 放宽 + Alembic 迁移

**Files:** Modify `service-hub/app/models.py`(CommandDispatchRequest.action Literal、+mode)、`app/db_models.py`(CommandModel.mode)、`app/store.py`(store_command 写 mode);Create `migrations/versions/20260620_xxxx_command_mode.py`;Test `tests/test_api.py`、`tests/test_db.py`

**Interfaces:**
- Produces:dispatch 接受 `action ∈ {update,restart,start,stop,force-restart,pull-redeploy}` + `mode: Literal["graceful","force"]|None`;CommandModel 持久化 `mode`;下发帧含 mode。

- [ ] **Step 1: 失败测试**:dispatch `{action:"stop",mode:"graceful",dir:...}` → 201 且 CommandModel.mode=="graceful";`action:"stop"` 当前应 422(未放宽前)→ 先红。
- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**。`models.py` action Literal 加四个新值 + `mode: Literal["graceful","force"]|None=None`;`db_models.py` CommandModel 加 `mode: Mapped[str|None]`;写 Alembic 迁移(add column commands.mode);store_command 落 mode;下发 payload 带 mode。autogenerate diff=0 核对。
- [ ] **Step 4:** 通过 + `alembic upgrade head` 干净 + 既有 hub 测试不回归。
- [ ] **Step 5: Commit** `feat(hub): dispatch 支持 start/stop/force-restart/pull-redeploy + mode 字段 + 迁移`

---

### Task 6（hub）：requested_by 服务端派生

**Files:** Modify `app/routers/commands.py`、`app/api_support.py`、`app/store.py`;Test `tests/test_api.py`

**Interfaces:**
- Produces:`_derive_requested_by(admin_token)->str`(据 token 关联固定身份,如 "platform-admin");dispatch/retry 用它覆盖 requested_by,X-Requested-By 仅记入 caller hint 字段。

- [ ] **Step 1: 失败测试**:带 `X-Requested-By: attacker` 调 dispatch → CommandModel.requested_by != "attacker"(=派生身份);caller hint 另记。
- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**。`_derive_requested_by` 由 admin token 映射身份(单 admin→固定串;多 token 时各自身份);commands.py 用派生值落 requested_by,X-Requested-By 存入新增 `caller_hint`(或日志)。
- [ ] **Step 4:** 通过。 **Step 5: Commit** `feat(hub): requested_by 由 admin token 服务端派生(不信任客户端头)`

---

### Task 7（hub）：force 服务端护栏（速率 + 最后健康实例不变式）

**Files:** Create `app/routers/node_ops.py` 或 Modify `commands.py`;Modify `app/store.py`;Test `tests/test_node_ops.py`

**Interfaces:**
- Produces:force stop/down(及 force-restart 视需要)走守卫:① 全局滑窗速率(env `FORCE_OP_MAX_PER_WINDOW`/`FORCE_OP_WINDOW_SEC`)超限→429/明确错误;② force stop 前调 list-instances,若停掉后该 service 健康实例归零→拒(除非显式 `allowLastInstance`)。

- [ ] **Step 1: 失败测试**:连发 N+1 次 force-down → 第 N+1 被速率拒;force stop 某 service 唯一健康实例 → 被拒。
- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**。滑窗计数(进程内,hub 单实例前提);停前 list-instances 算健康数;不变式校验。
- [ ] **Step 4:** 通过。 **Step 5: Commit** `feat(hub): force 操作服务端护栏(速率 + 不可停最后健康实例)`

---

### Task 8（hub，可选）：两套寻址对齐——match_instance 回 compose project label

**Files:** Modify `service-agent/services/instance_match.py`、`core/rolling.py`(list-instances 结果带 project);Test `service-agent/tests/test_instance_match.py`

**Interfaces:**
- Produces:list-instances 每实例增 `composeProject`(读容器 `com.docker.compose.project` label);BFF/hub 用它与 Service.dir 推得 project 比对,不一致标 `matched=false`。

- [ ] **Step 1: 失败测试**:match_instance 返回含 `composeProject`;label 与期望 project 不符 → matched=false。
- [ ] **Step 2-4:** 实现(docker inspect label)+ 通过。
- [ ] **Step 5: Commit** `feat(agent): list-instances 回 compose project label 供优雅/force 目标对齐`

---

### Task 9（platform BFF）：hub_client 扩展 + 节点聚合 /api/nodes

**Files:** Modify `service-platform/app/hub_client.py`、`app/models.py`;Create `app/routers/nodes.py`;Modify `app/main.py`;Test `tests/test_nodes.py`

**Interfaces:**
- Consumes:hub `GET /api/agents`、`call_agent list-instances`、Service 表(`store` 查 service)。
- Produces:`hub_client.list_agents()`、`hub_client.list_instances(agentId, serviceName)`;`GET /api/nodes` 返回 `{count, rows:[{agentId, serviceCode, namespaceCode, dir, defaultImage, nacosServiceName, online, lastSeen, healthyCount, degraded}], ...}`(camelCase 信封);**Service 表驱动 + per-agent fan-out + 单 agent 超时只标该行 degraded 不阻塞整页**。

- [ ] **Step 1: 失败测试**:mock hub_client + Service 表→/api/nodes 返 (agent×service) 行;某 agent list-instances 超时→该行 `degraded=true、healthyCount=null` 且其它行正常、整体 200。
- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**。遍历 Service 表→每行取 hub 在线态(list_agents 一次性)+ 按需 list-instances(并发 + 独立超时 try/except→degraded);require_session;HUB_ADMIN_TOKEN 服务端注入。
- [ ] **Step 4:** 通过(含降级用例)。 **Step 5: Commit** `feat(platform): /api/nodes(Service 表驱动 + 单 agent 超时降级)`

---

### Task 10（platform BFF）：节点操作下发 + 操作审计

**Files:** Modify `app/hub_client.py`、`app/routers/nodes.py`、`app/models.py`;Test `tests/test_nodes.py`

**Interfaces:**
- Produces:`POST /api/nodes/{agentId}/{serviceCode}/{action}`(body `{mode}`):查 Service 表得 dir/nacos_service_name→优雅 restart 走 hub `/api/rolling-restart`、其余走 hub dispatch(带 dir/mode/image[default_image]);requested_by 由 hub 派生(BFF 只传 token);`GET /api/node-operations`(代理 hub CommandModel + rolling 任务,camelCase 信封 + 服务端分页)。

- [ ] **Step 1: 失败测试**:POST restart+graceful→调 hub rolling-restart(agentId+serviceName);POST stop+force→调 hub dispatch(action=stop,mode=force,dir=Service.dir);非台账 service→404;/api/node-operations 返审计信封。
- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**。action→hub 端点路由(graceful restart/redeploy→rolling-restart;start/stop/force→dispatch);dir/image 取自 Service 表(不接受客户端传路径/任意 image);node-operations 聚合。
- [ ] **Step 4:** 通过。 **Step 5: Commit** `feat(platform): 节点操作下发(Service 表寻址)+ 操作审计端点`

---

### Task 11（SPA）：节点页

**Files:** Create `web/src/pages/NodesPage.tsx`;Modify `web/src/api/resources.ts`、`src/layout/AppShell.tsx`、`src/main.tsx`;Test `__tests__/nodes.test.tsx`

**Interfaces:**
- Consumes:`/api/nodes`、`/api/nodes/{agentId}/{serviceCode}/{action}`。
- Produces:节点页(ProTable 列 serviceCode/namespaceCode/dir/online/healthyCount/defaultImage;行操作 启动/停止/重启/拉镜像重部署,各 优雅/force + **二次确认输 serviceCode 核对**;操作后轮询结果);左侧「运维」组菜单 + lazy 路由。

- [ ] **Step 1: 失败测试**:mock resources→渲染节点行;点「停止」→选 force→确认框输 serviceCode→提交→断言调 `/api/nodes/.../stop` body `{mode:'force'}`;degraded 行健康显「-」。
- [ ] **Step 2-4:** 实现(复用 P1-SPA CrudTable/ShowOnce 范式 + api client 兜底)+ 通过 + lint/build/coverage 门。
- [ ] **Step 5: Commit** `feat(platform-web): 节点页(启停重启重部署 + 二次确认 + 实时态)`

---

### Task 12（SPA）：操作审计页

**Files:** Create `web/src/pages/NodeOperationsPage.tsx`;Modify resources/AppShell/main;Test `__tests__/node-operations.test.tsx`

- [ ] **Step 1: 失败测试**:mock `/api/node-operations`→只读 ProTable 列 who(派生)/action/mode/目标/状态/输出摘要 + 服务端分页;无 token→401。
- [ ] **Step 2-4:** 实现 + 通过 + 门。
- [ ] **Step 5: Commit** `feat(platform-web): 操作审计页`

---

### Task 13：集成验收

**Files:** Test 各仓 + 手动 WSL docker

- [ ] **Step 1:** 后端三仓 pytest 全绿(含覆盖率门);SPA vitest+lint+build 全绿。
- [ ] **Step 2:** WSL docker 起 agent+hub+platform，节点页对真实 compose 操作:启停重启重部署 优雅/force 如期;**优雅与 force 作用同一组容器(对账)**;停机服务可见可 start。
- [ ] **Step 3:** 安全验收:dir 越界/self-project/非白名单 image 被 agent 拒;force-down 触发速率/最后健康实例闸;requested_by 不可伪造;**P1-deploy 阻塞项:hub 读端点匿名 403 + 异网段直连被拒**。
- [ ] **Step 4:** 审计留痕完整(who 派生/action/mode/结果);单 agent 卡死不拖垮节点页。
- [ ] **Step 5: Commit** `test(node-control): 集成验收 + 安全门`

## Self-Review

- **Spec 覆盖**:启停重启重部署(T2/T3)、Service 表权威源(T9)、优雅复用 rolling(T10)、dir 守卫+self-project(T1)、image 白名单(T4)、mode+迁移(T5)、requested_by 派生(T6)、force 闸(T7)、两寻址对齐(T8)、BFF 降级(T9)、审计(T10/T12)、节点页/审计页(T11/T12)、P1-deploy 前置(T0/T13 门)、hub 单实例约束(Global)✅。
- **占位扫描**:每任务给了文件/接口/关键代码/测试/commit;TDD 先红后绿。
- **类型一致**:action/mode 值集跨 T5/T10 一致;dir 来源(Service 表)跨 T9/T10/agent 一致;graceful 复用 rolling 跨 T10/main 一致。
- **遗留**:list-projects(发现野 project)定为可选,本计划不含(节点页以 Service 表为源已足);实际运行 imageTag(docker inspect)可选,一期展示 default_image。
