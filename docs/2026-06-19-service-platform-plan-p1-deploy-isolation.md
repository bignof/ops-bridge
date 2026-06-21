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

> **拓扑决策(2026-06-21 用户定)**:采「compose 自带 nginx」——nginx 反代 platform UI(`/`)+ agent WS(`/ws/agent`);hub/platform **均不发布宿主机端口**,仅 nginx 发布 80/443。闭合 stub 旧草案(platform:8090 直发)未交代「hub 不发端口后远程 agent 怎么连 hub」的硬伤。cnp `/api/k8s/shutdown` 鉴权本轮同期改(见 Task 4,跨仓)。

```
service-platform/deploy/
  docker-compose.yml        # nginx + hub + platform; svc_internal 内网; hub/platform 无宿主机端口, 仅 nginx 发布 80/443
  nginx/nginx.conf          # / -> service-platform:8080; /ws/agent -> service-hub:8080 (WS upgrade)
  .env.example              # 栈级 env(镜像名/token/DB/JWT...)
  README.md                 # 隔离拓扑说明 + 运维注意(含 agent WS_URL 指向 nginx /ws/agent)
  scripts/validate_isolation.py   # 对抗:异网段直连 hub:8080/platform:8080 必须被拒; nginx->platform/hub 可达
# 同仓(service-hub, 已完成于 Task 3):
service-hub/app/routers/{agents,commands,logs}.py  # 只读+日志端点补 _require_admin_token ✅
service-hub/tests/test_readonly_auth.py            # ✅
# 跨仓(cnp, Task 4):
packages/plugins/@orchisky/plugin-service-k8s/...  # /api/k8s/shutdown 补鉴权 + agent 调用方携带凭据
```

---

### Task 1: 隔离拓扑（nginx 自带反代 + hub/platform 均不暴露宿主机面）

**Files:** Create `service-platform/deploy/docker-compose.yml`、`deploy/nginx/nginx.conf`、`deploy/.env.example`、`deploy/README.md`

**接口/参照(已核实)**:hub 现 compose `service-hub/docker-compose.yml`(env 驱动:`SERVICE_HUB_IMAGE`、`SERVICE_HUB_BIND_PORT`、`env_file: .env`、volume `/data/service-hub`、healthcheck GET `/health`)。platform env(`service-platform/app/config.py`):`HOST/PORT(8080)`、`DATABASE_URL`、`PLATFORM_ADMIN_USER/PASSWORD`、`PLATFORM_JWT_SECRET`(须 ≥32 字符否则启动被拒)、`SERVICE_HUB_URL`、`HUB_ADMIN_TOKEN`、`PLUGIN_STORAGE_DIR`。镜像名经 CI secret(`SERVICE_HUB_IMAGE_NAME`/`SERVICE_PLATFORM_IMAGE_NAME`),compose 用 env 占位(如 `${SERVICE_HUB_IMAGE}`/`${SERVICE_PLATFORM_IMAGE}`)。

- [ ] **Step 1: 写 compose**:`svc_internal` 内网承载 nginx↔platform↔hub;`service-hub`、`service-platform` **均不写 `ports`**(不向宿主机发布);`nginx` 服务 `ports: ["80:80","443:443"]`(唯一对外面),`depends_on` hub+platform。platform env `SERVICE_HUB_URL=http://service-hub:8080`。沿用 hub 的 healthcheck/volume/restart。
```yaml
# 关键片段(示意,实做按上面接口补全 env/volume/healthcheck)
networks:
  svc_internal: {}        # 内网; 不发布 hub/platform 端口即满足「异网段不可达」
services:
  nginx:
    image: nginx:1.27-alpine
    networks: [svc_internal]
    ports: ["80:80", "443:443"]
    volumes: ["./nginx/nginx.conf:/etc/nginx/nginx.conf:ro"]
    depends_on: [service-hub, service-platform]
  service-hub:
    image: ${SERVICE_HUB_IMAGE:?}
    networks: [svc_internal]      # 无 ports
    env_file: [.env]
  service-platform:
    image: ${SERVICE_PLATFORM_IMAGE:?}
    networks: [svc_internal]      # 无 ports
    environment:
      SERVICE_HUB_URL: http://service-hub:8080
    env_file: [.env]
```
  > **DB/Redis 出站注意**:若 hub/platform 的 `DATABASE_URL` 指向**外部 MySQL**,网络**不能** `internal: true`(会断外联)。普通 bridge + 不发布端口已满足「异网段直连被拒」;`internal: true` 仅在 hub/platform 全用容器内 sqlite/无外部依赖时才用。实做据 DATABASE_URL 现状择一,README 写明取舍。
- [ ] **Step 2: nginx.conf**:`location /` → `proxy_pass http://service-platform:8080`;`location /ws/agent`(及 agent WS 路径)→ `proxy_pass http://service-hub:8080` + WS 升级头(`Upgrade`/`Connection`、`proxy_http_version 1.1`、长读超时)。按需把 `/api`(平台 API)归 platform。443 段可留证书挂载占位(README 注明)。
- [ ] **Step 3: README** 画拓扑:运维浏览器→nginx(443/80)→[svc_internal] platform;**远程 agent → nginx `/ws/agent` → [svc_internal] hub**(agent `WS_URL` 由直连 hub:8080 改指向 nginx);hub/platform 无宿主机面。.env.example 列栈级 env(镜像名/ADMIN_TOKEN/HUB_ADMIN_TOKEN/PLATFORM_*/DATABASE_URL)。
- [ ] **Step 4: commit** `feat(platform-deploy): 自带 nginx 反代, hub/platform 均不暴露宿主机面`

---

### Task 2: 网段对抗测试（异网段直连 hub/platform 必须被拒, nginx 可达）

**Files:** Create `service-platform/deploy/scripts/validate_isolation.py`

- [ ] **Step 1: 脚本**(仿 `service-hub/scripts/validate_*_e2e.py`):① 经 nginx `GET http://<host>:80/health`(platform)+ agent WS 路径可达 → 期望通(nginx 反代成立);② 从**宿主机/其它网段**直连 `http://<host>:8080`(hub)与 platform 内部端口 → 期望**连接被拒/超时**(未发布);③(可选)容器内 `service-hub` DNS 仅 svc_internal 可解析。脚本打印各项结论,任一不符 exit 1。
- [ ] **Step 2: 跑法**:`docker compose -f deploy/docker-compose.yml up -d` 后,宿主机直接跑(外网项 + 经 nginx 项);内网项可 `docker exec` nginx/platform 容器内验。
- [ ] **Step 3: 验收**:nginx 可达 platform/hub-ws、外部网段直连 hub:8080/platform 内部端口均连不上。
- [ ] **Step 4: commit** `feat(platform-deploy): hub 网段隔离对抗验证脚本`

---

### Task 3: （跨仓 service-hub）只读 + 日志端点补 admin token

**Files(service-hub 仓):** Modify `app/routers/{agents,commands,logs}.py`(GET /api/agents、GET /api/agents/{id}、GET /api/commands*、POST /api/agents/{id}/logs/stream 首行加 `_require_admin_token`);Create `tests/test_readonly_auth.py`

> 纵深防御:即便网络隔离,补鉴权消除"内网任意对端匿名读"。platform 调这些端点本就带 `X-Admin-Token`(读端点带 token 多余但无害),故不破坏 platform。

- [ ] **Step 1: 失败测试**`tests/test_readonly_auth.py`:不带 X-Admin-Token 调 `GET /api/agents`、`GET /api/commands`、`POST /api/agents/x/logs/stream` → 期望 403;带正确 token → 200/正常。
- [ ] **Step 2:** 各端点 handler 首行 `_require_admin_token(admin_token)`(签名加 `admin_token: str|None = Header(alias="X-Admin-Token")`,照 dispatch/rolling 既有写法)。
- [ ] **Step 3:** 跑 hub 全量(`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q`)绿;确认既有 platform/agent 调用仍带 token。
- [ ] **Step 4: commit(service-hub 仓)** `fix(hub): 只读 + 日志端点补 admin token(纵深防御, 消除内网匿名读)`

> **状态:✅ 已完成**(commit `5e31dbd`,六只读/日志端点补鉴权,12 新测试 + hub 全量 96 passed,评审 Approved)。

---

### Task 4（跨仓 cnp）：`/api/k8s/shutdown` 补鉴权 + 调用方携带凭据

> **跨仓**:本任务在 **cnp 仓**(`C:\Users\bigno\Documents\work\orchisky\src\cnp`,`@orchisky/plugin-service-k8s`),**不在 services-monorepo**。cnp 分支规范:off `release/1.7.x`(非本仓 main);commit 中文(前缀英文)+ cnp 的 Co-Authored-By / Claude-Session footer;**绝不碰客户库、不擅自 push**。
> **协调风险(核心)**:`/api/k8s/shutdown` 现被 agent 优雅 drain(`service-agent/core/rolling.py`,services-monorepo)调用——给端点加鉴权会**断掉 rolling/drain**,除非调用方同步携带凭据。这是 cnp↔services-monorepo 的协调改动。

- [ ] **Step 1: 先调研(动手前必做)**:① 读 cnp `@orchisky/plugin-service-k8s` 找 `/api/k8s/shutdown` handler,确认现有鉴权/ACL scope(很可能 `public` 或无校验);② 读 services-monorepo `service-agent/core/rolling.py` + graceful 链,确认 agent 现在**如何**调该端点(URL、有无 header/token、`_validate_health_base_url` 限制);③ 判定鉴权模型:候选=共享密钥 header(agent 与 worker 共配一个 env token)/ 内网 IP allowlist / NocoBase 既有鉴权中间件。把结论写入本任务再实现。
- [ ] **Step 2: cnp 端实现**:按调研结论给 `/api/k8s/shutdown` 加鉴权(默认拒绝匿名);保留 worker 自身/集群内合法调用路径;补 cnp 侧测试。**业务插件交付以 `yarn build @orchisky/plugin-service-k8s --tar` 为准**(若 @orchisky 也走 tar;否则按该包既有验收)。
- [ ] **Step 3: agent 端配合(services-monorepo)**:agent drain 调用 `/api/k8s/shutdown` 时携带 Step 2 约定的凭据(新增 env,如 `K8S_SHUTDOWN_TOKEN`);补/改 `test_rolling.py`/`test_graceful.py` 断言带凭据;**不得破坏 main 现有 rolling 行为**。
- [ ] **Step 4: 验收**:匿名调 `/api/k8s/shutdown` → 拒;带凭据(agent 路径)→ 通;rolling/drain 端到端不回归。
- [ ] **Step 5: commit**:cnp 仓 `fix(service-k8s): /api/k8s/shutdown 补鉴权(消除匿名优雅停机触发)`;services-monorepo `feat(agent): drain 调用 /api/k8s/shutdown 携带凭据`。两仓分别提交、分别 PR。

---

## Self-Review

- 覆盖 spec 第 1 号安全控制:hub 不暴露宿主机面(T1)+ 网段对抗测试(T2)+ 只读端点补鉴权(T3,纵深)✅。
- 不破坏:agent 出站 WS、platform→hub 内网调用、token 兼容 ✅。
- 跨仓边界:T3 在 service-hub 仓(另分支/PR),计划已标注。
- 取舍:若团队决定把隔离归入独立部署批次,须在 spec 把 M-3 从 P1 验收门正式移走;否则本计划即该验收门的承接。
