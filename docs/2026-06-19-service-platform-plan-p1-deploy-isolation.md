# service-platform P1-部署/隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 或 executing-plans。Steps 用 checkbox。
> 本计划承接 ultracode 评审「横切硬伤:hub 隔离(M-3)+ 网段对抗测试 — spec 第 1 号安全控制,三计划无人认领」。**P1 验收门的一部分**,不可省。

**Goal:** 把外部 service-hub 从"任意网络对端可匿名读"收紧为"仅 platform + agent 网络可达",并补上 spec 要求的网段对抗测试;同时推动给 hub 的零鉴权只读端点补 admin token。

**Architecture:** hub 不再把 8080 发布到宿主机 0.0.0.0;hub 与 platform 经共享 **internal docker network** 互通(agent 经反连 WS,不需 hub 入站发布);对抗脚本仿 `service-hub/scripts/validate_*_e2e.py` 验证异网段直连被拒。hub 只读端点补鉴权属跨仓(service-hub)改进项,本计划登记+给最小实现。

**Tech Stack:** docker compose、Python(对抗脚本,httpx/socket)、(跨仓)service-hub FastAPI。

## Global Constraints

- 依据 spec `2026-06-18-service-platform-design.md`:安全总览#1、部署节(hub 隔离硬约束)、测试节(网段对抗)。
- **核到的现状**(评审已实测):hub `docker-compose.yml` 仍 `ports: "8080:8080"`(0.0.0.0)+ 无 networks 块;`GET /api/agents`、`GET /api/commands*`、`POST /api/agents/{id}/logs/stream` **零鉴权**(匿名网络对端可读全机群 agent 清单/命令历史/实时日志=信息泄露;dispatch/rolling 已 token-gated)。
- 不破坏现有:agent 经 WS **反向**连 hub(出站),隔离不影响;platform 服务端调 hub 走内网。
- 提交中文 `feat(platform-deploy): ...` / `fix(hub): ...`(跨仓注明);分支 `feat/service-platform`(hub 改动若在 service-hub 仓另开分支)。

## File Structure

```
service-platform/deploy/
  docker-compose.yml        # platform + hub 同一 compose, 共享 internal network; hub 不发布宿主机端口(或仅 127.0.0.1)
  README.md                 # 隔离拓扑说明 + 运维注意
  scripts/validate_isolation.py   # 对抗:异网段/宿主机外直连 hub:8080 必须连接被拒; platform->hub 可达
# 跨仓(service-hub):
service-hub/app/api_support.py 或 routers/{agents,commands,logs}.py  # 给只读+日志端点补 _require_admin_token
service-hub/tests/test_readonly_auth.py
```

---

### Task 1: 隔离拓扑（hub 不暴露宿主机面 + 共享 internal network）

**Files:** Create `service-platform/deploy/docker-compose.yml`、`deploy/README.md`

- [ ] **Step 1: 写 compose**:定义 internal network `svc_internal`(`internal: true` 或至少不发布 hub 端口);`service-hub` 服务**去掉 `ports` 发布**(或改 `127.0.0.1:8080:8080` 仅本机),加入 `svc_internal`;`service-platform` 服务加入 `svc_internal`(可访问 `http://service-hub:8080`)+ 对运维发布自己的端口(如 `8090:8080`,经 nginx)。env `SERVICE_HUB_URL=http://service-hub:8080`。
```yaml
# 关键片段(示意)
networks:
  svc_internal:
    internal: true
services:
  service-hub:
    image: <hub-image>
    networks: [svc_internal]
    # 不写 ports: 不向宿主机/公网发布
  service-platform:
    image: <platform-image>
    networks: [svc_internal, default]
    environment:
      SERVICE_HUB_URL: http://service-hub:8080
    ports:
      - "8090:8080"   # 仅这层暴露给运维(再经 nginx)
```
- [ ] **Step 2: README** 画拓扑:运维→platform(8090)→(internal)→hub;agent→(出站 WS)→hub;说明"hub 无入站宿主机面"。
- [ ] **Step 3: commit** `feat(platform-deploy): hub+platform 共享 internal network, hub 不暴露宿主机面`

---

### Task 2: 网段对抗测试（异网段直连 hub 必须被拒）

**Files:** Create `service-platform/deploy/scripts/validate_isolation.py`

- [ ] **Step 1: 脚本**(仿 `service-hub/scripts/validate_*_e2e.py`):① 从 internal 网络内(platform 容器内)`GET http://service-hub:8080/health` → 期望 200(platform 可达);② 从**宿主机/其它网段**直连 `http://<host>:8080/api/agents` → 期望**连接被拒/超时**(TCP 不可达,因未发布)。脚本打印两项结论,任一不符 exit 1。
- [ ] **Step 2: 跑法**:`docker compose -f deploy/docker-compose.yml up -d` 后,容器内 `docker exec service-platform python scripts/validate_isolation.py`(内网项)+ 宿主机直接跑(外网项)。
- [ ] **Step 3: 验收**:两项均通过(platform 可达 hub、外部网段连不上 hub)。
- [ ] **Step 4: commit** `feat(platform-deploy): hub 网段隔离对抗验证脚本`

---

### Task 3: （跨仓 service-hub）只读 + 日志端点补 admin token

**Files(service-hub 仓):** Modify `app/routers/{agents,commands,logs}.py`(GET /api/agents、GET /api/agents/{id}、GET /api/commands*、POST /api/agents/{id}/logs/stream 首行加 `_require_admin_token`);Create `tests/test_readonly_auth.py`

> 纵深防御:即便网络隔离,补鉴权消除"内网任意对端匿名读"。platform 调这些端点本就带 `X-Admin-Token`(读端点带 token 多余但无害),故不破坏 platform。

- [ ] **Step 1: 失败测试**`tests/test_readonly_auth.py`:不带 X-Admin-Token 调 `GET /api/agents`、`GET /api/commands`、`POST /api/agents/x/logs/stream` → 期望 403;带正确 token → 200/正常。
- [ ] **Step 2:** 各端点 handler 首行 `_require_admin_token(admin_token)`(签名加 `admin_token: str|None = Header(alias="X-Admin-Token")`,照 dispatch/rolling 既有写法)。
- [ ] **Step 3:** 跑 hub 全量(`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q`)绿;确认既有 platform/agent 调用仍带 token。
- [ ] **Step 4: commit(service-hub 仓)** `fix(hub): 只读 + 日志端点补 admin token(纵深防御, 消除内网匿名读)`

---

## Self-Review

- 覆盖 spec 第 1 号安全控制:hub 不暴露宿主机面(T1)+ 网段对抗测试(T2)+ 只读端点补鉴权(T3,纵深)✅。
- 不破坏:agent 出站 WS、platform→hub 内网调用、token 兼容 ✅。
- 跨仓边界:T3 在 service-hub 仓(另分支/PR),计划已标注。
- 取舍:若团队决定把隔离归入独立部署批次,须在 spec 把 M-3 从 P1 验收门正式移走;否则本计划即该验收门的承接。
