# service-platform P1a（平台后端本体）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 FastAPI 从零建 `service-platform/` 后端:单 admin 登录(JWT) + 台账 CRUD + 插件上传/发布/回滚(单活 + 事务锁) + 兼容现 sync-plugins 的分发查询/下载端点 + per-namespace 拉包鉴权 + 真 MySQL8 DB 约束。**= 现 NocoBase 分发平台后端的对等替代(不含前端 SPA / 存量迁移,各自单独计划)。**

**Architecture:** **镜像同仓 `service-hub/` 的技术栈与目录范式**(FastAPI + SQLAlchemy 2.0 + alembic + pytest)。新目录 `service-platform/`,与 `service-hub/`、`service-agent/` 并列。后端原生调外部 `service-hub`(仅命名空间 provision/rotate;命令/滚动/日志属 P2)。台账落机群 MySQL8 的独立库 `service_platform`;插件包落本地卷。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2.0(Mapped/mapped_column)、alembic、PyMySQL、PyJWT、httpx、pytest。**参照实现一律看 `service-hub/` 同名文件**(config.py/db.py/db_models.py/main.py/migrations/env.py/tests/{conftest,test_api}.py)。**依赖一律钉版本**(照 service-hub `requirements.txt` 全 pin 范式;`service-hub` 不用 JWT 故无 PyJWT,本平台须自钉,如 `PyJWT==2.10.1`——评审「pin PyJWT」)。

## Global Constraints

- 配套 spec(权威): `services-monorepo/docs/2026-06-18-service-platform-design.md` v3。真实旧库 schema: 同目录 `collections.sql`/`fields.sql`/`uiSchemas.sql`(**注意文件名是 `uiSchemas.sql` 大写 S**——大小写敏感 FS 下错名找不到;本计划只读参考,不在 P1a 迁移)。
- 平台 DB = 机群 **MySQL8** 独立库 `service_platform`(`DATABASE_URL=mysql+pymysql://.../service_platform`)。**测试用每测临时文件 sqlite**(经 Task 1 的 `client` fixture 注入,见下「测试地基」;**禁用 `sqlite:///:memory:` 单例**——跨用例/跨线程状态泄漏,会瓦解 TDD 与任务独立性,评审 B2),所有 DDL/约束必须 sqlite + MySQL8 双可建——**故单活用 app 维护的 nullable unique 普通列,禁用 MySQL 生成列**(评审 M-4)。
- **单活不变式**:每 `(service_id, plugin_id)` 至多一行 `is_active=True`,靠 nullable unique 列 `spv_active_key`(active 时=`f"{service_id}-{plugin_id}"`,否则 NULL) + DB UNIQUE 兜底;发布/回滚在 `select(ServicePlugin).with_for_update()` 单事务内"先全置非活+清 key,再置目标活+设 key",捕 `IntegrityError`→409。
- **version 不变式**:`plugin_version.version` **NOT NULL** 且**必须=.tgz 内 `package.json.version`**(非文件名派生);分发响应 `version` 字段恒非空(评审 M-2/High-2)。
- **分发下载防 IDOR/穿越**:下载只收**不可变 `attachment_id`**(不收自由 path);鉴权**归属式**——pull token 反解 namespace,校验 `attachment→plugin_version→spv(active)→service→namespace == token.namespace`,不符 **404**;落盘路径平台生成,不用客户端 filename 拼(评审 High-1/B5)。
- **鉴权分两套**:① 人类会话 = `Authorization: Bearer <JWT>`(单 admin,env 凭据,常量时间比较,免 CSRF);② 节点分发 = per-namespace **pull token**(Bearer,独立路径)。`HUB_ADMIN_TOKEN` 仅服务端持有,不下发浏览器。
- 参数/错误:FastAPI 路由 + Pydantic;错误抛 `HTTPException`;敏感串(token/agentKey)严禁记日志。
- 提交:conventional commits 中文(`feat(platform): ...`);提交前 `git branch --show-current` 确认在 `feat/service-platform` 分支;**勿 push**。
- 测试命令(cwd=`service-platform`):`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q`。无覆盖率门,但每动一处配测试。
- **测试地基(评审 B2,权威范式 = `service-hub/tests/test_api.py` 的 `client` fixture,非 conftest setdefault)**:`conftest.py` **只**注 `sys.path`(照 `service-hub/tests/conftest.py`,**不放 `os.environ.setdefault`**);config 读 env 的兜底由该模块顶部 **一行** `os.environ.setdefault("ADMIN_TOKEN"...)` 式注入(service-hub 放在 `test_api.py` 顶,见其第 15 行),**真正的 DB 隔离靠 `client` fixture**:每测 `Database("sqlite:///"+str(tmp_path/"test.db"))`(临时文件库)+ `init_schema()` + `import app.main as main_module` 后 swap `main_module.database`(及任何模块级单例引用)+ 用 `object.__setattr__(main_module.settings, "<字段>", <值>)` 改 frozen 配置 + `with TestClient(app) as c: yield c`,退出 `database.engine.dispose()` 并还原 swap。**所有经 `TestClient` 的端点测试与所有直调 `store.*` 的测试都必须经此 fixture 注入临时文件库**(否则 `:memory:` 误隔离 / `no such table`)。Task 1 给出 fixture 模板并作为验收门。
- **database 单例规格(评审 L2/M10,Task 1 即落定,不留执行期)**:`database = Database(settings.database_url)` 实例**建在 `app/main.py`**(照 service-hub:`main.py` 持 `database`);`store.py` / `routers/*` **一律函数内延迟** `import app.main as main_module` 后取 `main_module.database`,**禁止任何模块级** `from app.main import database`(循环 import + import 期绑定使 per-test 换库失效)。Task 1 `Interfaces` 的 Produces 写 `app.main.database`,与 Task 6 对齐(不再自创 `app.db.database` 单例)。

## 跨计划契约(在 P1a 钉死;P1-SPA / P1b 依赖,务必照此实现)

> 以下为三份计划共享的接口契约,**P1a 是权威产出方**;改动须三份计划同步。

- **请求/响应模型全 camelCase(评审 H2)**:所有请求/响应 Pydantic 模型统一 `ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)`(**照搬 `service-hub/app/models.py` 的 `to_camel` + `MODEL_CONFIG`**);响应**禁止**手搓 snake dict——统一定义 `XxxOut(BaseModel)` 并 `response_model=` 或 `model_validate(...)` 序列化(store 返回的 snake dict 经模型转 camel)。Pydantic v2 默认 `extra='ignore'`:前端送错 key 的可选字段会被**静默丢弃**(列静默 NULL 且逃过 smoke),故契约不容漂移。
- **分发端点字段名固定(sync-plugins 兼容,评审 H2;已核 `serviceHub/query.ts` + `sync-plugins.js`)**:`GET /api/distribution/plugins` 响应恒为数组 `[{pluginName, version, url}]`(三字段 camel,与现 `queryPlugin` 完全一致,`url = PLUGIN_DOWNLOAD_BASE_URL + 相对 url`);查询参数用**小写** `namespace` / `service`(对应 `n.namespaceCode` / `s.serviceCode`)。这两处**不走 to_camel 改名**,保持与节点 `sync-plugins.js` 字面兼容。
- **台账列表 LEFT JOIN 回可读名(评审 H3)**:台账 list 端点除裸外键 id 外,**必须 LEFT JOIN 关联表带只读名称列**(与分发端点 join 做法一致,**不发明客户端解析层**):
  - `services` list → `namespaceCode`
  - `service-plugins` list → `namespaceCode` / `serviceCode` / `pluginCode`
  - `releases`(spv)list → `serviceCode` / `pluginCode` / `version`(+ `namespaceCode`)
  - `fetch-records` list → 各 `*Code` / `version`
  - `namespaces` list → `code` + `name`(`name` 空回退 `code`,前端用 `code` 作稳定标签)
- **列表响应信封 + 服务端分页(评审 M2)**:列表统一 `{count, rows, page, pageSize, totalPage}`,支持 `page` / `pageSize` 查询参数。**`fetch-records` 必须服务端分页**(审计表无界);低基数配置表(namespace/plugin 等)可全量返回但须在该端点注明「前端 ProTable 客户端分页」。
- **releases list filter 语义(评审 H4,**不新建聚合端点**)**:`GET /api/releases` —— 不传 filter 或 `isActive=true` → 主表(每 `(service,plugin)` 绑定一行 active);传 `serviceId` + `pluginId` → 该绑定的版本历史。同一 spv list 端点服务两种视图(对齐旧「插件发布」页:主表 `filter[isActive]=yes`,历史抽屉按 pluginId+isActive=no)。
- **级联过滤端点(评审 M3)**:`services` list 加 `?namespaceId=`;`service-plugins` list 加 `?serviceId=`(与 `plugin-versions` 的 `?pluginId=` 同形)。旧平台靠 NocoBase 关联选择自动发服务端 filter,手写 ProForm 无此 magic,故服务端须支持。
- **默认拒绝中间件守 `/api/**`(评审 H6)**:落 HTTP 中间件对 `/api/**` 统一 default-deny(校验 JWT),白名单放行 `/auth/login`、`/api/distribution/**`(改由 pull token 校验)、`/health`;逐路由 `Depends(require_session)` **保留作纵深防御**(不删)。见 Task 3.5。

## File Structure

```
service-platform/
  requirements.txt              # 全 pin(照 service-hub):fastapi uvicorn sqlalchemy alembic pymysql PyJWT httpx python-multipart + dev: pytest httpx
  alembic.ini                   # 照搬 service-hub/alembic.ini,script_location=migrations
  Dockerfile                    # P1a: python:3.12-slim + uvicorn(多阶段含 SPA 留给 P1-SPA 计划)
  .env.example
  app/
    __init__.py
    config.py                   # frozen Settings(env)
    db.py                       # Database(engine/session_factory/init_schema=alembic upgrade)
    db_models.py                # 8 个 SQLAlchemy 模型
    models.py                   # Pydantic 请求/响应模型(全 camelCase:to_camel + MODEL_CONFIG,照 service-hub/app/models.py)
    auth.py                     # JWT 签发/校验 + require_session 依赖 + 常量时间登录校验
    middleware.py               # default-deny:守 /api/**(白名单 /auth/login、/api/distribution/**、/health),评审 H6
    hub_client.py               # httpx 调 service-hub(provision/rotate agent;P2 再加命令/滚动)
    store.py                    # DB 访问层:泛型 CRUD helpers + publish/rollback 事务 + 分发查询(函数内延迟 import app.main 取 database)
    tokens.py                   # pull token 生成/哈希/校验(per-namespace)
    storage.py                  # 本地卷包存储:落盘(平台生成路径)/读取流/.tgz 校验+解析 package.json
    main.py                     # FastAPI app + database 单例 + lifespan(init_schema) + add_middleware + include_router
    routers/
      __init__.py
      system.py                 # GET /health
      auth.py                   # POST /auth/login, GET /auth/me
      namespaces.py             # CRUD + rotate-key + rotate-pull-token
      services.py               # CRUD(?namespaceId= 级联过滤)
      plugins.py                # CRUD
      plugin_versions.py        # list(?pluginId=)/get + upload
      service_plugins.py        # list(?serviceId= 级联过滤)/create/destroy(绑定)
      releases.py               # publish / reactivate / rollback / spv list(isActive filter 语义见契约)
      fetch_records.py          # GET /api/fetch-records(服务端分页 + LEFT JOIN 名称),评审 H1
      distribution.py           # GET /api/distribution/plugins, GET /api/distribution/download/{id}
  migrations/
    env.py                      # 照搬 service-hub/migrations/env.py(import app.db_models)
    versions/
      20260619_0001_initial_schema.py
  tests/
    conftest.py                 # 照搬 service-hub/tests/conftest.py:仅 sys.path 注入(无 os.environ.setdefault)
    test_*.py                   # client fixture(tmp_path 文件库 + swap main_module.database)见 Task 1
```

---

### Task 1: 工程骨架 + config + db + FastAPI + /health

**Files:**
- Create: `service-platform/requirements.txt`、`app/__init__.py`、`app/config.py`、`app/db.py`、`app/main.py`、`app/routers/__init__.py`、`app/routers/system.py`、`alembic.ini`、`migrations/env.py`、`migrations/versions/.gitkeep`、`tests/__init__.py`、`tests/conftest.py`、`tests/test_health.py`、`.env.example`
- Reference(照抄结构): `service-hub/app/{config,db,main}.py`、`service-hub/migrations/env.py`、`service-hub/tests/{conftest,test_api}.py`、`service-hub/alembic.ini`

**Interfaces:**
- Produces: `app.config.settings`(Settings 实例)、`app.db.Database`(`.engine`/`.session_factory`/`.init_schema()`)、`app.db.Base`、`app.main.app`(FastAPI)、**`app.main.database`(Database 单例,唯一落点;评审 M10/L2——`store`/`routers` 函数内延迟 `import app.main as main_module` 取,不创建 `app.db.database`)**。

- [ ] **Step 1: requirements.txt(全 pin,照 service-hub 范式;版本号执行时取当时稳定版)**
```
fastapi==0.115.8
uvicorn[standard]==0.34.0
SQLAlchemy==2.0.39
alembic==1.14.1
PyMySQL==1.1.1
PyJWT==2.10.1
httpx==0.28.1
python-multipart==0.0.20
```
> dev 依赖照 service-hub 拆 `requirements-dev.txt`(`-r requirements.txt` + `pytest==8.3.5` + `httpx==0.28.1`)。**禁止裸 `PyJWT`(不钉版本)**:PyJWT ≤2.10.x 在空密钥时静默接受可被绕过 JWT,钉版 + 空密钥拒绝启动(见 Task 3)双保险(评审「pin PyJWT」+ 被否决条目残留 hardening)。

- [ ] **Step 2: app/config.py**(frozen dataclass,照 service-hub 范式;含本平台全部 env)
```python
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8080"))
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./service-platform.db")
    admin_user: str = os.getenv("PLATFORM_ADMIN_USER", "")
    admin_password: str = os.getenv("PLATFORM_ADMIN_PASSWORD", "")
    jwt_secret: str = os.getenv("PLATFORM_JWT_SECRET", "")
    jwt_ttl_seconds: int = int(os.getenv("PLATFORM_JWT_TTL", "28800"))  # 8h
    service_hub_url: str = os.getenv("SERVICE_HUB_URL", "")
    hub_admin_token: str = os.getenv("HUB_ADMIN_TOKEN", "")
    plugin_storage_dir: str = os.getenv("PLUGIN_STORAGE_DIR", "./data/plugins")
    plugin_download_base_url: str = os.getenv("PLUGIN_DOWNLOAD_BASE_URL", "")


settings = Settings()
```

- [ ] **Step 3: app/db.py**(照搬 service-hub/app/db.py:`Database` 类,`_managed_tables` 改为本平台 8 表,`init_schema` 同款 stamp/upgrade 逻辑)。`_managed_tables = {"namespace","service","plugin","plugin_version","plugin_attachment","service_plugin","service_plugin_version","fetch_record"}`。`Base = DeclarativeBase` 子类。

- [ ] **Step 4: alembic.ini + migrations/env.py**:`alembic.ini` 照搬 service-hub(`script_location = migrations`);`migrations/env.py` 照搬(`from app import db_models` + `target_metadata = Base.metadata` + `compare_type=True`)。

- [ ] **Step 5: app/routers/system.py + app/main.py**
```python
# routers/system.py
from fastapi import APIRouter
router = APIRouter()

@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}
```
```python
# app/main.py
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.config import settings
from app.db import Database
from app.routers.system import router as system_router

logger = logging.getLogger(__name__)
database = Database(settings.database_url)   # 评审 M10/L2:database 单例唯一落点(store/routers 函数内延迟 import app.main 取),不在 app/db.py 建

@asynccontextmanager
async def lifespan(_: FastAPI):
    # 评审(被否决条目残留 hardening):空/过短 jwt_secret 拒绝启动(纵深防御)
    if not settings.jwt_secret or len(settings.jwt_secret) < 32:
        raise RuntimeError("PLATFORM_JWT_SECRET 未配置或过短(须 ≥32 字符)")
    database.init_schema()
    yield

app = FastAPI(title="service-platform", version="0.1.0", lifespan=lifespan)
# Task 3.5 起:app.add_middleware(SessionGuardMiddleware)
app.include_router(system_router)
```

- [ ] **Step 6: tests/conftest.py(评审 B2:照搬 `service-hub/tests/conftest.py`——**只**注 sys.path,**不放 `os.environ.setdefault`**;env 兜底放各测试模块顶部一行,DB 隔离靠下方 `client` fixture)**
```python
# tests/conftest.py —— 仅 sys.path 注入(对齐 service-hub/tests/conftest.py)
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parent.parent
root_str = str(ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)
```
  **`client` fixture(本任务建立的测试骨架,后续所有端点测试与直调 store 的测试都用它;模板照 `service-hub/tests/test_api.py:24-45`):**
```python
# 放进 conftest.py 或共享测试工具模块;每个用例独立 tmp_path 文件库,退出还原
import os
from pathlib import Path
from typing import Iterator
import pytest
from fastapi.testclient import TestClient

# config 在 import app.config 时读 env,故进程级兜底一次(照 service-hub test_api.py 顶部 setdefault)
os.environ.setdefault("PLATFORM_ADMIN_USER", "admin")
os.environ.setdefault("PLATFORM_ADMIN_PASSWORD", "admin-pw")
os.environ.setdefault("PLATFORM_JWT_SECRET", "test-secret-which-is-long-enough-0123456789")

from app.db import Database
from app.main import app

@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    database = Database("sqlite:///" + str(tmp_path / "test.db"))
    database.init_schema()
    import app.main as main_module
    old_database = main_module.database
    main_module.database = database                     # swap 单例(store/routers 函数内取 main_module.database)
    object.__setattr__(main_module.settings, "service_hub_url", "")  # frozen 字段一律 object.__setattr__
    with TestClient(app) as c:
        yield c
    database.engine.dispose()
    main_module.database = old_database                 # 还原,防跨用例泄漏
```

- [ ] **Step 7: 写失败测试 tests/test_health.py(用 `client` fixture,不再裸 `TestClient(app)`)**
```python
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

- [ ] **Step 8: 跑测试**`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_health.py -q` → 先红(模块缺失)→补齐到绿。
- [ ] **验收门(评审 B2)**:`client` fixture 必须就绪且 `health` 测试经它通过;后续 Task(3/6/9/10/11)的端点测试与直调 `store.*` 的测试**一律经此 fixture**——禁止裸 `TestClient(app)` 或 `:memory:` 单例。

- [ ] **Step 9: commit** `git add service-platform && git commit -m "feat(platform): FastAPI 骨架 + config/db/health + alembic env"`

---

### Task 2: 数据模型 + 初始迁移(8 表 + 真 DB 约束)

**Files:** Create `app/db_models.py`、`migrations/versions/20260619_0001_initial_schema.py`、`tests/test_models.py`

**Interfaces:**
- Produces: 模型类 `Namespace, Service, Plugin, PluginVersion, PluginAttachment, ServicePlugin, ServicePluginVersion, FetchRecord`(均 `from app.db import Base`);字段命名见下。

- [ ] **Step 1: db_models.py**(SQLAlchemy 2.0,照 service-hub/db_models.py 范式)
```python
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base

class Namespace(Base):
    __tablename__ = "namespace"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(255), unique=True, index=True)   # =agentId
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)      # 别名
    pull_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class Service(Base):
    __tablename__ = "service"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace_id: Mapped[int] = mapped_column(Integer, index=True)
    service_code: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)      # 别名
    dir: Mapped[str | None] = mapped_column(String(2048), nullable=True)      # compose 目录(命令下发)
    default_image: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    nacos_service_name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 滚动用(新增)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("namespace_id", "service_code", name="uq_service_ns_code"),)

class Plugin(Base):
    __tablename__ = "plugin"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(255), unique=True, index=True)   # npm 包名
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)      # 别名
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class PluginVersion(Base):
    __tablename__ = "plugin_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[str] = mapped_column(String(255))   # NOT NULL;= package.json.version
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("plugin_id", "version", name="uq_pv_plugin_version"),)

class PluginAttachment(Base):
    __tablename__ = "plugin_attachment"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_version_id: Mapped[int] = mapped_column(Integer, index=True)
    filename: Mapped[str] = mapped_column(String(512))
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_path: Mapped[str] = mapped_column(String(1024))   # 平台生成
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class ServicePlugin(Base):
    __tablename__ = "service_plugin"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("service_id", "plugin_id", name="uq_sp_service_plugin"),)

class ServicePluginVersion(Base):
    __tablename__ = "service_plugin_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_version_id: Mapped[int] = mapped_column(Integer, index=True)
    version_order: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_rolled_back: Mapped[bool] = mapped_column(Boolean, default=False)
    spv_active_key: Mapped[str | None] = mapped_column(String(512), nullable=True, unique=True)  # 单活
    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class FetchRecord(Base):
    __tablename__ = "fetch_record"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace_id: Mapped[int] = mapped_column(Integer, index=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_version_id: Mapped[int] = mapped_column(Integer, index=True)
    fetch_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 2: 初始迁移**`migrations/versions/20260619_0001_initial_schema.py`(`revision="20260619_0001"`, `down_revision=None`;照 service-hub 迁移范式 `op.create_table` 建 8 表 + `UniqueConstraint`(uq_service_ns_code/uq_pv_plugin_version/uq_sp_service_plugin)+ `spv_active_key` 列 `unique=True` + 各 index)。`version` 列 `nullable=False`。

- [ ] **Step 3: 失败测试 tests/test_models.py**(用内存 sqlite 起 Database + init_schema,验约束)
```python
import os
import pytest
from datetime import datetime, timezone
from sqlalchemy.exc import IntegrityError
from app.db import Database
from app import db_models as m

def _db(tmp_path):
    d = Database(f"sqlite:///{tmp_path}/t.db"); d.init_schema(); return d

def test_unique_namespace_code(tmp_path):
    d = _db(tmp_path); now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add(m.Namespace(code="ns1", created_at=now, updated_at=now)); s.commit()
    with d.session_factory() as s:
        s.add(m.Namespace(code="ns1", created_at=now, updated_at=now))
        with pytest.raises(IntegrityError): s.commit()

def test_spv_single_active_unique(tmp_path):
    d = _db(tmp_path); now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add(m.ServicePluginVersion(service_plugin_id=1, service_id=1, plugin_id=2, plugin_version_id=10,
              version_order=1, is_active=True, spv_active_key="1-2", created_at=now, updated_at=now)); s.commit()
    with d.session_factory() as s:
        s.add(m.ServicePluginVersion(service_plugin_id=1, service_id=1, plugin_id=2, plugin_version_id=11,
              version_order=2, is_active=True, spv_active_key="1-2", created_at=now, updated_at=now))
        with pytest.raises(IntegrityError): s.commit()   # 同 (service,plugin) 不能两行 active

def test_spv_multiple_inactive_ok(tmp_path):
    d = _db(tmp_path); now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add_all([
            m.ServicePluginVersion(service_plugin_id=1, service_id=1, plugin_id=2, plugin_version_id=10,
                version_order=1, is_active=False, spv_active_key=None, created_at=now, updated_at=now),
            m.ServicePluginVersion(service_plugin_id=1, service_id=1, plugin_id=2, plugin_version_id=11,
                version_order=2, is_active=False, spv_active_key=None, created_at=now, updated_at=now),
        ]); s.commit()   # 多个 NULL active_key 允许
```

- [ ] **Step 4: 跑** `pytest tests/test_models.py -q` → 红→实现到绿。
- [ ] **Step 5: commit** `feat(platform): 8 表数据模型 + 初始迁移 + 真 DB 约束(单活 nullable unique)`

---

### Task 3: 鉴权(单 admin 登录 + JWT + require_session)

**Files:** Create `app/auth.py`、`app/routers/auth.py`、`tests/test_auth.py`;Modify `app/main.py`(include auth_router)

**Interfaces:**
- Produces: `app.auth.issue_token(sub:str)->str`、`app.auth.require_session`(FastAPI 依赖,返回 sub 或抛 401)、`app.auth.verify_login(user:str, pw:str)->bool`。

- [ ] **Step 1: 失败测试 tests/test_auth.py(用 `client` fixture)**
```python
def test_login_ok_and_me(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"})
    assert r.status_code == 200
    tok = r.json()["token"]; assert tok
    r2 = client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r2.status_code == 200 and r2.json()["user"] == "admin"

def test_login_wrong_password_401(client):
    assert client.post("/auth/login", json={"username": "admin", "password": "x"}).status_code == 401

def test_me_without_token_401(client):
    assert client.get("/auth/me").status_code == 401
```

- [ ] **Step 2: app/auth.py(评审 Nit-2:`require_session` 强制 require sub/exp,缺 sub 返 401 而非 KeyError→500)**
```python
import hmac, time
import jwt
from fastapi import Header, HTTPException, status
from app.config import settings

def verify_login(user: str, pw: str) -> bool:
    if not settings.admin_user or not settings.admin_password:
        return False
    return hmac.compare_digest(user, settings.admin_user) and hmac.compare_digest(pw, settings.admin_password)

def issue_token(sub: str) -> str:
    now = int(time.time())
    payload = {"sub": sub, "iat": now, "exp": now + settings.jwt_ttl_seconds}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")

def require_session(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization[len("Bearer "):]
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"],
                             options={"require": ["sub", "exp"]})   # Nit-2:缺字段→DecodeError→401
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    return sub
```
> **空密钥 hardening(评审被否决条目残留)**:已 pin `PyJWT==2.10.1`(空密钥时 encode/decode 抛 `InvalidKeyError`,fail-closed);**额外**在 `app/main.py` 启动(或 lifespan)校验 `settings.jwt_secret` 非空且长度 ≥32,否则 `raise RuntimeError` 拒绝启动(纵深防御,可选但建议)。

- [ ] **Step 3: app/routers/auth.py**
```python
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from app.auth import verify_login, issue_token, require_session
router = APIRouter()

class LoginReq(BaseModel):
    username: str
    password: str

@router.post("/auth/login")
async def login(req: LoginReq):
    if not verify_login(req.username, req.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return {"token": issue_token(req.username)}

@router.get("/auth/me")
async def me(sub: str = Depends(require_session)):
    return {"user": sub}
```

- [ ] **Step 4:** main.py `include_router(auth_router)`;跑 `pytest tests/test_auth.py -q` 到绿。
- [ ] **Step 5: commit** `feat(platform): 单 admin 登录 + JWT(Authorization header)+ require_session 依赖`

---

### Task 3.5: default-deny 中间件（守 /api/**,评审 H6）

**Files:** Create `app/middleware.py`、`tests/test_middleware.py`;Modify `app/main.py`(`app.add_middleware(...)` 在 include_router 后/前均可,中间件按注册逆序执行,确保它最外层先跑)

**Interfaces:**
- Produces: `app.middleware.SessionGuardMiddleware`(ASGI/HTTP 中间件)——对 path 命中 `/api/**` 的请求强制校验 JWT(复用 `auth.require_session` 的解析逻辑),白名单**前缀**放行:`/auth/login`、`/api/distribution/`(改由 pull token 在各 distribution 端点内校验)、`/health`。校验失败返回 401(缺/坏 JWT)。

**为什么(spec L100 + 评审 H6)**:spec 要求中间件守 `/api/**`(default-deny),而非仅靠逐路由 `Depends`。service-hub 的读端点(`GET /api/agents` 等)正因只在写端点挂 token、读端点裸奔而 fail-open——本平台**不复制该 bug**。逐路由 `Depends(require_session)` **保留作纵深防御**(双层)。

- [ ] **Step 1: 失败测试 tests/test_middleware.py(用 `client` fixture)**
```python
# 1) 故意挂一个“占位 /api 路由且不加 Depends”(测试内动态 app.add_api_route 或在 routers 里留一个无 Depends 的内部端点),
#    无 Authorization 调它 → 仍 401(证明中间件守住,而非靠路由自己的 Depends)
# 2) 白名单:GET /health 无 token → 200;POST /auth/login 无 token → 可达(凭据错则 401,非中间件拦截)
# 3) /api/distribution/plugins 无 JWT → 不被中间件 401 拦(交给端点内 pull token 校验,无 token 时由端点返回 403/401)
# 4) 带合法 JWT 的 /api/plugins → 放行(200)
```
> 占位路由实现建议:在 `tests/test_middleware.py` 里用 `app.add_api_route("/api/__probe__", lambda: {"ok": True})`(无依赖),验证中间件在 Depends 缺席时仍拦截;测试末尾清理(或用独立 app 实例)。

- [ ] **Step 2: app/middleware.py**(`BaseHTTPMiddleware` 子类:`path.startswith` 命中 `/api/` 且不在白名单前缀 → 解析 `Authorization` Bearer,失败 `JSONResponse(status_code=401)`;成功 `await call_next`)。白名单前缀集中常量维护,**新增公开 /api 端点须显式加白名单**(审计可见)。
- [ ] **Step 3:** main.py `app.add_middleware(SessionGuardMiddleware)`;绿。 **Step 4: commit** `feat(platform): default-deny 中间件守 /api/**(白名单 login/distribution/health)`

---

### Task 4: hub_client(命名空间用 provision/rotate)

**Files:** Create `app/hub_client.py`、`tests/test_hub_client.py`

**Interfaces:**
- Produces: `app.hub_client.provision_agent(agent_id:str)->str`(返回 agentKey,POST hub `/api/agents` 带 `X-Admin-Token`,读返回 `agentKey`)、`rotate_agent_key(agent_id:str)->str`(POST `/api/agents/{id}/credentials/rotate`,读 `agentKey`)。错误抛 `HubError`。

- [ ] **Step 1: 失败测试**(monkeypatch httpx,断言 URL/header/返回解析;参照 service-hub 测试不引第三方 mock)
```python
import app.hub_client as hc

class _Resp:
    def __init__(self, data): self._d = data
    def raise_for_status(self): pass
    def json(self): return self._d

def test_provision_agent(monkeypatch):
    calls = {}
    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url; calls["headers"] = headers; calls["json"] = json
        return _Resp({"agentKey": "k-123"})
    monkeypatch.setattr(hc.httpx, "post", fake_post)
    # 评审 H8:settings 是 frozen dataclass,raising=False 救不了(底层仍 FrozenInstanceError)。
    # 用 object.__setattr__ 改 frozen 字段(照 service-hub/tests/test_api.py:36);teardown 由 monkeypatch 之外手动还原,
    # 或更稳妥:monkeypatch.setattr(hc, "settings", types.SimpleNamespace(service_hub_url="http://hub:8080", hub_admin_token="T"))
    import types
    monkeypatch.setattr(hc, "settings", types.SimpleNamespace(service_hub_url="http://hub:8080", hub_admin_token="T"))
    assert hc.provision_agent("ns1") == "k-123"
    assert calls["url"].endswith("/api/agents")
    assert calls["headers"]["X-Admin-Token"] == "T"
    assert calls["json"]["agentId"] == "ns1"
```
> **评审 H8(权威范式 = service-hub)**:`settings` 是 `@dataclass(frozen=True)`,`monkeypatch.setattr(<frozen 实例>, attr, val, raising=False)` 会抛 `FrozenInstanceError`(`raising` 只控属性不存在时是否报错,不绕 frozen),`undo()` 还原时二次抛错污染同 run。两种正确写法:① `object.__setattr__(module.settings, "field", value)`(teardown 还原,见 `client` fixture);② `monkeypatch.setattr(<module>, "settings", types.SimpleNamespace(...))` 整体替换模块引用(`hub_client.py`/`storage.py` 都是 `from app.config import settings` 模块级引用,可行)。**全计划测试统一这两种,禁 `raising=False` 写法。**

- [ ] **Step 2: app/hub_client.py**
```python
import httpx
from app.config import settings

class HubError(Exception): ...

def _headers() -> dict:
    return {"Content-Type": "application/json", "X-Admin-Token": settings.hub_admin_token}

def provision_agent(agent_id: str) -> str:
    if not settings.service_hub_url:
        raise HubError("SERVICE_HUB_URL 未配置")
    r = httpx.post(f"{settings.service_hub_url}/api/agents", headers=_headers(),
                   json={"agentId": agent_id}, timeout=15)
    r.raise_for_status()
    key = r.json().get("agentKey")
    if not key:
        raise HubError("hub 未返回 agentKey")
    return key

def rotate_agent_key(agent_id: str) -> str:
    r = httpx.post(f"{settings.service_hub_url}/api/agents/{agent_id}/credentials/rotate",
                   headers=_headers(), json={}, timeout=15)
    r.raise_for_status()
    key = r.json().get("agentKey")
    if not key:
        raise HubError("hub 未返回 agentKey")
    return key
```

- [ ] **Step 3:** 跑测试到绿。 **Step 4: commit** `feat(platform): hub_client(provision/rotate agent, 带 admin token)`

---

### Task 5: tokens（per-namespace pull token 生成/哈希/校验）

**Files:** Create `app/tokens.py`、`tests/test_tokens.py`

**Interfaces:**
- Produces: `app.tokens.new_pull_token()->tuple[str,str]`(返回 `(明文, sha256哈希)`)、`hash_token(plain:str)->str`、`verify_token(plain:str, stored_hash:str)->bool`(`hmac.compare_digest`)。

- [ ] **Step 1: 失败测试**
```python
from app import tokens

def test_pull_token_roundtrip():
    plain, h = tokens.new_pull_token()
    assert plain and len(plain) >= 32 and h != plain
    assert tokens.verify_token(plain, h) is True
    assert tokens.verify_token("wrong", h) is False
```

- [ ] **Step 2: app/tokens.py**
```python
import hashlib, hmac, secrets

def new_pull_token() -> tuple[str, str]:
    plain = secrets.token_urlsafe(32)
    return plain, hash_token(plain)

def hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()

def verify_token(plain: str, stored_hash: str) -> bool:
    if not plain or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(plain), stored_hash)
```
- [ ] **Step 3:** 绿。 **Step 4: commit** `feat(platform): per-namespace pull token 生成/哈希/校验`

---

### Task 6a: store.py 泛型 helper + Pydantic camelCase 模型 + plugin 完整 CRUD（无 hub）

> **评审 M9**:原 Task 6 把 store 基础设施 + 单例落定 + 4 资源 CRUD + hub 集成揉进一任务/一提交,评审面过宽。拆 **6a(本地基础设施 + plugin CRUD,纯本地无 hub)→ 6b(namespace 含 hub/show-once + service + service_plugin)**,各一组测试一次提交,依赖 6a→6b。

**Files:** Create `app/store.py`、`app/models.py`(Pydantic)、`app/routers/plugins.py`、`tests/test_crud_plugin.py`;Modify `app/main.py`(include plugins router)

**Interfaces:**
- Produces: `store` 泛型 helper `list_rows/get_row/create_row/update_row/delete_row`(基于 model + `main_module.database`)、`store.Conflict`(自定义异常,路由映射 409);`app.models` 的 `to_camel` + `MODEL_CONFIG`(全计划 Pydantic 模型共用)+ `PluginIn/PluginOut`;路由 `/api/plugins`(GET 列表 `{count,rows,page,pageSize,totalPage}` / POST 201)、`/api/plugins/{id}`(GET/PATCH/DELETE 204)。

- [ ] **Step 1: plugin 失败测试 tests/test_crud_plugin.py(用 `client` fixture;响应全 camelCase)**
```python
def _h(client):
    tok = client.post("/auth/login", json={"username":"admin","password":"admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}

def test_plugin_crud(client):
    h = _h(client)
    r = client.post("/api/plugins", json={"code":"@business/plugin-x","name":"X"}, headers=h)
    assert r.status_code == 201; pid = r.json()["id"]
    body = client.get("/api/plugins", headers=h).json()
    assert body["count"] >= 1 and "rows" in body and "totalPage" in body   # 信封形状
    assert client.get(f"/api/plugins/{pid}", headers=h).json()["code"] == "@business/plugin-x"
    assert client.patch(f"/api/plugins/{pid}", json={"name":"X2"}, headers=h).json()["name"] == "X2"
    assert client.delete(f"/api/plugins/{pid}", headers=h).status_code == 204
    client.post("/api/plugins", json={"code":"dup"}, headers=h)
    assert client.post("/api/plugins", json={"code":"dup"}, headers=h).status_code == 409   # 唯一约束

def test_plugin_requires_auth(client):
    assert client.get("/api/plugins").status_code == 401
```

- [ ] **Step 2: app/models.py(全计划 Pydantic 基座,照搬 `service-hub/app/models.py`)**
```python
from pydantic import BaseModel, ConfigDict

def to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])

MODEL_CONFIG = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)

class PluginOut(BaseModel):
    model_config = MODEL_CONFIG
    id: int
    code: str
    name: str | None = None

class PluginIn(BaseModel):
    model_config = MODEL_CONFIG
    code: str
    name: str | None = None
# 各资源 In/Out 模型均带 model_config = MODEL_CONFIG;列表信封 ListEnvelope[T]={count,rows,page,pageSize,totalPage}
```
> **评审 H2 / 跨计划契约**:**响应禁止手搓 snake dict**,一律经 `*Out` 模型(`response_model=` 或 `.model_dump(by_alias=True)`)序列化成 camelCase。store 返回 ORM/snake,路由转模型。

- [ ] **Step 3: app/store.py 泛型 CRUD helper(评审 M10:函数内延迟 import,禁模块级 `from app.main import database`)**
```python
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

class Conflict(Exception): ...

def _now(): return datetime.now(timezone.utc)

def _db():
    import app.main as main_module    # 延迟取单例:避免循环 import + 使 per-test 换库生效(评审 M10)
    return main_module.database

def list_rows(model, *, page: int = 1, page_size: int = 20, filters: list | None = None):
    with _db().session_factory() as s:
        base = select(model)
        cnt = select(func.count()).select_from(model)
        for cond in (filters or []):
            base = base.where(cond); cnt = cnt.where(cond)
        count = s.execute(cnt).scalar_one()
        rows = s.execute(base.offset((page-1)*page_size).limit(page_size)).scalars().all()
        return rows, count
# get_row / create_row(捕 IntegrityError→raise Conflict) / update_row / delete_row 同款,均用 _db()
```
> **评审 M10**:`from app.main import database` 模块级 import **删除**——它是真循环 import,且 import 期绑定让 `client` fixture 的换库失效。统一函数内 `import app.main as main_module`(照 service-hub `store.py`/`routers`)。Task 1 已把 `database` 单例落在 `app/main.py`,**不创建 `app.db.database`**。

- [ ] **Step 4: app/routers/plugins.py**(`response_model=PluginOut` / 列表信封;create 201、delete 204、`store.Conflict`→409)。列表参数 `page/pageSize`。
- [ ] **Step 5:** main.py include plugins router;跑 plugin 测试到绿。
- [ ] **Step 6: commit** `feat(platform): store 泛型 helper + Pydantic camelCase 基座 + plugin CRUD`

---

### Task 6b: namespace(含 hub provision/show-once)+ service + service_plugin CRUD

**Files:** Create `app/routers/{namespaces,services,service_plugins}.py`、扩 `app/models.py`、`tests/test_crud_namespace.py`、`tests/test_crud_service.py`;Modify `app/main.py`(include 三 router)

**说明**:三资源 CRUD 与 plugin 同构,按**字段表 + 各自特例**实现(特例逐条列出,非"similar to")。所有写依赖 `require_session`。响应全 camelCase(经 `*Out` 模型)。

- [ ] **Step 1: namespace/service/service_plugin 失败测试 + 实现**(字段表 + 特例):

| 资源 | 路由前缀 | 字段(create/update) | 列表返回(LEFT JOIN 名称,评审 H3) | 特例 |
| --- | --- | --- | --- | --- |
| namespace | /api/namespaces | code(必填,唯一), name | id, code, name(`name` 空回退 code;在线/心跳留空,P2 实时读 hub 填) | create 先 `hub_client.provision_agent(code)` 取 agentKey,**仅放进 create 响应 `agentKey` 字段、不入库**(show-once);见 Task 7 rotate 子端点 |
| service | /api/services | namespaceId(必填), serviceCode(必填), name, dir, defaultImage, nacosServiceName | 全字段 + **`namespaceCode`**(LEFT JOIN namespace);**支持 `?namespaceId=` 级联过滤**(评审 M3) | UNIQUE(namespace_id, service_code)→409 |
| service_plugin | /api/service-plugins | serviceId, pluginId | id, serviceId, pluginId + **`namespaceCode`/`serviceCode`/`pluginCode`**(LEFT JOIN);**支持 `?serviceId=` 级联过滤**(评审 M3) | 仅 list/create/destroy(无 update);UNIQUE(service_id,plugin_id)→409 |

> **请求体字段名(评审 H2)**:create/update 请求体一律 camelCase(`namespaceId`/`serviceCode`/`defaultImage`/`nacosServiceName`),经 `*In(model_config=MODEL_CONFIG)` 接收(`populate_by_name=True` 故 snake 也兼容,但契约以 camel 为准)。

- [ ] **Step 2: namespace create 的 hub monkeypatch(评审 H7,与 Task 7 rotate 测试一致)**
```python
def test_namespace_create_provisions_and_shows_key_once(client, monkeypatch):
    import app.hub_client as hc
    monkeypatch.setattr(hc, "provision_agent", lambda code: "fake-key")   # H7:不打桩则 SERVICE_HUB_URL 未配→HubError→500
    h = _h(client)
    r = client.post("/api/namespaces", json={"code":"ns1","name":"NS1"}, headers=h)
    assert r.status_code == 201
    assert r.json()["agentKey"] == "fake-key"           # show-once 返回明文
    nid = r.json()["id"]
    # Nit-1:重查该行,断言不含 agentKey 明文(库内无该列/不落地)
    got = client.get(f"/api/namespaces/{nid}", headers=h).json()
    assert "agentKey" not in got and "fake-key" not in str(got)
```
> **评审 H7**:namespace create 调 `hub_client.provision_agent(code)`,测试**必须** `monkeypatch.setattr(hc, "provision_agent", lambda code: "fake-key")`,否则 `SERVICE_HUB_URL` 未配 → `HubError` → 500,断言 201 必红(与 Task 7 rotate 测试同款打桩)。**评审 Nit-1**:show-once 后重查行断言不含 agentKey 明文(守 show-once 不变式)。

- [ ] **Step 3:** 为 service / service_plugin 补与 plugin 同形 CRUD 测试(唯一约束 409、无 token 401)+ **级联过滤断言**(`?namespaceId=` / `?serviceId=` 真过滤,非 mock 形状)+ **LEFT JOIN 名称列断言**。
- [ ] **Step 4: commit** `feat(platform): 台账 CRUD(namespace 含 hub show-once / service / service_plugin)+ 级联过滤 + JOIN 名称`

---

### Task 7: 命名空间 rotate-key + rotate-pull-token(show-once)

**Files:** Modify `app/routers/namespaces.py`、`app/store.py`;Create 测试 `tests/test_namespace_rotate.py`

- [ ] **Step 1: 失败测试(用 `client` fixture)**(monkeypatch `hub_client.rotate_agent_key`——模块函数非 frozen 字段,`monkeypatch.setattr(hc, "rotate_agent_key", lambda code: "k2")` 即可,写法无误;断言响应含 key、库里只存 pull_token_hash 不存明文)
```python
def test_rotate_pull_token_returns_once_and_stores_hash(client):
    # 先建 namespace(create 同样需 monkeypatch provision_agent,见 Task 6b)
    # POST /api/namespaces/{id}/rotate-pull-token → 200 {pullToken: <明文>}
    # 库内 namespace.pull_token_hash != 明文,且 verify_token(明文, hash) True;重查行不含明文(Nit-1)
```

- [ ] **Step 2: 实现两个子端点**
  - `POST /api/namespaces/{id}/rotate-key`:调 `hub_client.rotate_agent_key(ns.code)`,返回 `{agentKey: <明文>}`(不入库)。
  - `POST /api/namespaces/{id}/rotate-pull-token`:`plain, h = tokens.new_pull_token()`,`update namespace.pull_token_hash = h`,返回 `{pullToken: plain}`(明文仅此一次)。
- [ ] **Step 3:** 绿。 **Step 4: commit** `feat(platform): 命名空间轮换 agentKey/pull token(show-once)`

---

### Task 8: storage（.tgz 校验/解析 package.json + 平台生成路径落盘/读流）

**Files:** Create `app/storage.py`、`tests/test_storage.py`

**Interfaces:**
- Produces: `app.storage.parse_tgz(data:bytes)->dict`(返回 `{name, version}`,**优先读 tar 内 `package/package.json`,缺则回退根级 `package.json`**——评审 B1;非法 tgz / 缺 package.json / 缺 version 抛 `BadPackage`)、`store_tgz(plugin_id:int, version_id:int, filename:str, data:bytes)->str`(落盘到 `<storage>/<plugin_id>/<version_id>/<sanitized>.tgz`,返回 storage_path)、`open_stream(storage_path:str)`(返回可迭代字节流;路径必须在 storage 根内,否则抛)。

> **评审 B1(已核真实 `.tgz` + `sync-plugins.js`)**:NocoBase `build --tar` 产物(如 `storage/tar/@business/plugin-mom-print-*.tgz`)首条目即**根级 `package.json`**(README/dist/... 全在根,**无 `package/` 前缀**);只有 `npm pack` 才把内容塞进 `package/` 子目录。节点脚本 `sync-plugins.js:236-238` 正是 `contentDir = fs.existsSync(packageSubdir) ? packageSubdir : extractDir`(package/ 优先,否则根)。`parse_tgz` 必须**同源对齐**做此回退,否则全量真实数据 100% `BadPackage`(而 fixture 用 `package/` 布局会假绿)。

- [ ] **Step 1: 失败测试 tests/test_storage.py(fixture 同时覆盖根级与 package/ 两种布局 + 穿越对抗 + 炸弹守卫;评审 B1/H5/L3)**
```python
import io, tarfile, json
import pytest
from app import storage

def _make_tgz(name, version, *, prefix=""):   # prefix="" → 根级布局(真实 build --tar);prefix="package/" → npm pack
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        content = json.dumps({"name": name, "version": version}).encode()
        info = tarfile.TarInfo(prefix + "package.json"); info.size = len(content)
        t.addfile(info, io.BytesIO(content))
    return buf.getvalue()

def test_parse_tgz_root_level_layout():          # B1:真实 build --tar(根级)必须能解析
    meta = storage.parse_tgz(_make_tgz("@business/plugin-x", "1.2.3"))
    assert meta == {"name": "@business/plugin-x", "version": "1.2.3"}

def test_parse_tgz_package_prefix_layout():      # npm pack(package/)也兼容
    meta = storage.parse_tgz(_make_tgz("@business/plugin-x", "1.2.3", prefix="package/"))
    assert meta == {"name": "@business/plugin-x", "version": "1.2.3"}

def test_parse_tgz_rejects_garbage():
    with pytest.raises(storage.BadPackage):
        storage.parse_tgz(b"not a tgz")

def test_store_and_open(tmp_path, monkeypatch):
    # 评审 H8:storage.settings 是 frozen,整体替换模块引用(禁 raising=False)
    import types
    monkeypatch.setattr(storage, "settings", types.SimpleNamespace(plugin_storage_dir=str(tmp_path)))
    p = storage.store_tgz(1, 10, "x.tgz", b"bytes")
    assert b"bytes" == b"".join(storage.open_stream(p))

def test_open_stream_rejects_traversal(tmp_path, monkeypatch):   # 评审 H5:穿越/绝对路径必 raise
    import types
    monkeypatch.setattr(storage, "settings", types.SimpleNamespace(plugin_storage_dir=str(tmp_path)))
    with pytest.raises(storage.BadPackage):
        list(storage.open_stream("../../../etc/passwd"))
    with pytest.raises(storage.BadPackage):
        list(storage.open_stream("/etc/passwd"))

def test_sanitize_strips_traversal():           # 评审 H5:_sanitize 结果不含 .. 与分隔符
    out = storage._sanitize("../../x")
    assert ".." not in out and "/" not in out and "\\" not in out
```

- [ ] **Step 2: app/storage.py**(tarfile 解析,**根级回退**;`store_tgz` 平台生成路径 + basename 白名单;`open_stream` realpath 防穿越;**解 package.json 前校验 member.size 防解压炸弹**——评审 L3)
```python
import io, json, os, re, tarfile
from app.config import settings

class BadPackage(Exception): ...

MAX_PKG_JSON_SIZE = 1 * 1024 * 1024   # package.json 上限 1MB,防解压炸弹(评审 L3)

def parse_tgz(data: bytes) -> dict:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = None
            # 评审 B1:package/ 优先(npm pack),回退根级(build --tar),与 sync-plugins.js 同源
            for n in ("package/package.json", "./package/package.json", "package.json", "./package.json"):
                try: member = t.getmember(n); break
                except KeyError: continue
            if member is None:
                raise BadPackage("缺 package.json(package/ 与根级均无)")
            if member.size is not None and member.size > MAX_PKG_JSON_SIZE:   # L3:防炸弹
                raise BadPackage("package.json 过大")
            pkg = json.loads(t.extractfile(member).read().decode())
    except BadPackage: raise
    except Exception as e:
        raise BadPackage(f"非法 .tgz: {e}") from None
    name, version = pkg.get("name"), pkg.get("version")
    if not name or not version:
        raise BadPackage("package.json 缺 name/version")
    return {"name": name, "version": version}

def _sanitize(filename: str) -> str:
    base = os.path.basename(filename or "plugin.tgz")
    base = re.sub(r"[^A-Za-z0-9._@+-]", "_", base)
    return base or "plugin.tgz"

def store_tgz(plugin_id: int, version_id: int, filename: str, data: bytes) -> str:
    rel = os.path.join(str(plugin_id), str(version_id), _sanitize(filename))
    abspath = os.path.join(settings.plugin_storage_dir, rel)
    os.makedirs(os.path.dirname(abspath), exist_ok=True)
    with open(abspath, "wb") as f: f.write(data)
    return rel   # 库里存相对路径

def open_stream(storage_path: str):
    root = os.path.realpath(settings.plugin_storage_dir)
    abspath = os.path.realpath(os.path.join(root, storage_path))
    if not (abspath == root or abspath.startswith(root + os.sep)):
        raise BadPackage("路径越界")
    if not os.path.isfile(abspath):
        raise FileNotFoundError(storage_path)
    def _gen():
        with open(abspath, "rb") as f:
            while chunk := f.read(65536): yield chunk
    return _gen()
```
> **评审 H5 补充**:`open_stream` 已对 realpath 越界 raise(测试 `test_open_stream_rejects_traversal` 守住);**上传请求体大小上限**在 Task 9 端点处理(`UploadFile` 读入前限大小)+ README 注明依赖 nginx `client_max_body_size`(评审 L3)。

- [ ] **Step 3:** 绿(含根级/package、穿越、炸弹用例)。 **Step 4: commit** `feat(platform): 插件包存储(.tgz 解析含根级回退 + 生成路径落盘 + 防穿越读流 + 炸弹守卫)`

---

### Task 9: 插件上传(version=package.json + 匹配 plugin + 入版本/附件)

**Files:** Create `app/routers/plugin_versions.py`、扩 `app/store.py`、`tests/test_upload.py`;Modify main.py

**Interfaces:**
- `POST /api/plugin-versions/upload`(multipart:file=.tgz):**先校验请求体/文件大小上限**(评审 L3,如 200MB,超限 413);解析 `package.json`→version;按 `name` 匹配 `plugin.code`(精确或 `LIKE %/<尾段>`,0/多命中→400);`(plugin_id, version)` 查重→409;落盘→建 plugin_version(version=package.json.version,NOT NULL)+ plugin_attachment(storage_path)。返回 camelCase `{pluginVersionId, attachmentId, version}`(经 `*Out` 模型)。
- `GET /api/plugin-versions?pluginId=` list(信封 `{count,rows,page,pageSize,totalPage}`,评审 M2/P1-SPA 上传页 ProTable 依赖此形状)/ `GET /api/plugin-versions/{id}`。

- [ ] **Step 1: 失败测试(用 `client` fixture)**(先建 plugin code=@business/plugin-x,再 upload `_make_tgz("@business/plugin-x","1.2.3")`(根级布局);断言 version=1.2.3、再传同 version→409;传未知包名→400;list 返回信封形状)
- [ ] **Step 2: 实现**(用 `python-multipart`,FastAPI `UploadFile`;**读入前判大小上限**;事务内建 version+attachment;先建 version 拿 id 再 `store_tgz(plugin_id, version_id, filename, data)` 回填 storage_path)
- [ ] **Step 3:** 绿。 **Step 4: commit** `feat(platform): 插件上传(version=package.json + 匹配 plugin + 版本/附件 + 大小上限)`

---

### Task 10: 发布/历史激活/回滚（单活 + 事务锁 + 状态机）

**Files:** Create `app/routers/releases.py`、扩 `app/store.py`(三个事务函数)、`tests/test_releases.py`;Modify main.py

**Interfaces:**
- `store.publish(service_id, plugin_id, plugin_version_id)`、`store.reactivate(spv_id)`、`store.rollback(spv_id)`;路由 `POST /api/releases/publish`、`/reactivate`、`/rollback`、`GET /api/releases`(filter 语义见下,**不新建聚合端点**)。
- **`GET /api/releases` filter 语义(评审 H4 / 跨计划契约)**:不传 filter 或 `isActive=true` → **主表**(每 `(service,plugin)` 绑定一行 active,对齐旧「插件发布」主表 `filter[isActive]=yes`);传 `serviceId`+`pluginId` → 该绑定**版本历史**(对齐历史抽屉)。响应信封 `{count,rows,page,pageSize,totalPage}`,**rows 经 LEFT JOIN 带 `serviceCode`/`pluginCode`/`version`(+`namespaceCode`)只读名称**(评审 H3)。

> **评审 L1(已实测)**:`with_for_update()` 在 sqlite 是 **no-op**(静默不加锁),真正的并发闸是 `spv_active_key` 的 **UNIQUE + `IntegrityError`→409**(sqlite/MySQL8 都每语句即时检查)。MySQL8 上 `with_for_update()` 才真行锁。**故 commit/Self-Review 措辞不得声称"测了 FOR UPDATE"**——sqlite 测的是 UNIQUE 闸;FOR UPDATE 留 skip-unless-mysql 集成测试(可选)。

- [ ] **Step 1: 失败测试 tests/test_releases.py(用 `client` fixture / 直调 store 也经 fixture 换库)**
```python
# 1) publish v10 → 唯一 active 是 v10, versionOrder=1, spv_active_key="<sid>-<pid>"
# 2) publish v11 → active=v11, v10 inactive(key=None), versionOrder=2
# 3) rollback(当前 active=v11) → active 回到 v10, v11 is_rolled_back=True
# 4) rollback 跳过已回滚: 候选谓词 versionOrder< 当前 ∧ not is_rolled_back ∧ not is_active
# 5) reactivate(历史 spv) → 该行 active + is_rolled_back 清 False
# 6) 并发幂等: 直接两行 active 插入被 DB UNIQUE 挡(已在 Task2 验);publish 内部先全灭活再置活
# 7) 【评审 M4 新增,必撞 UNIQUE 复现】当前 active = 后发布的高 PK 行,rollback / reactivate 到先发布的低 PK 历史行:
#    publish(v10)→publish(v11)→rollback() 期望回到 v10(现 6 条覆盖不到此爆炸路径,且是最常见回滚场景)
# 8) releases list:不传 filter → 主表每绑定一行 active;传 serviceId+pluginId → 该绑定历史;rows 含 serviceCode/pluginCode/version
```

- [ ] **Step 2: store.publish/reactivate/rollback**(`with _db().session_factory() as s:`(评审 M10 延迟 import,见 6a)+ `select(ServicePlugin)...with_for_update()` 锁行;**评审 M4:"清所有 key"与"置目标 key"之间强制 `s.flush()`**)
```python
def publish(service_id, plugin_id, plugin_version_id):
    with _db().session_factory() as s:
        sp = s.execute(select(ServicePlugin).where(
            ServicePlugin.service_id==service_id, ServicePlugin.plugin_id==plugin_id
        ).with_for_update()).scalar_one_or_none()
        if sp is None: raise NotFound("service_plugin 未绑定")
        # 先全灭活 + 清 key
        for row in s.execute(select(ServicePluginVersion).where(
            ServicePluginVersion.service_id==service_id, ServicePluginVersion.plugin_id==plugin_id
        )).scalars().all():
            row.is_active = False; row.spv_active_key = None
        s.flush()   # 评审 M4:先把"清 key"刷到 DB,再 INSERT 带 key 的新行,避免 SQLAlchemy 主键升序排序致 UNIQUE 立即违例
        max_order = s.execute(select(func.coalesce(func.max(ServicePluginVersion.version_order), 0)).where(
            ServicePluginVersion.service_plugin_id==sp.id)).scalar_one()
        now = _now()
        s.add(ServicePluginVersion(service_plugin_id=sp.id, service_id=service_id, plugin_id=plugin_id,
            plugin_version_id=plugin_version_id, version_order=max_order+1, is_active=True,
            is_rolled_back=False, spv_active_key=f"{service_id}-{plugin_id}", publish_time=now,
            created_at=now, updated_at=now))
        try: s.commit()
        except IntegrityError: s.rollback(); raise Conflict("并发发布冲突")
# reactivate / rollback 同样:先 UPDATE 全灭活 + 清 key → **s.flush()(评审 M4 关键!)** → 再置目标行 is_active=True + spv_active_key 设。
#   不 flush 的话,回滚到低 PK 历史行时 SQLAlchemy 按主键升序发 UPDATE,低 PK 的"置 key"先于高 PK 的"清 key"→ UNIQUE 立即违例(sqlite+MySQL8 都炸,即测试 #7)。
# reactivate: 全灭活+清 key → flush → 目标行 is_active=True + key 设 + is_rolled_back=False
# rollback: 校验 spv 为当前 active;候选=versionOrder<当前 ∧ not is_rolled_back ∧ not is_active 里 max(versionOrder);
#           全灭活+清 key → flush → 当前 is_rolled_back=True;候选 is_active=True + key 设
```

- [ ] **Step 3:** 绿(8 条全过,尤其 #7 高 PK→低 PK 回滚不撞 UNIQUE)。 **Step 4: commit** `feat(platform): 发布/历史激活/回滚(单活+事务+清key与置key间flush+链表不变式)`

---

### Task 11: 分发端点（queryPlugin 兼容 + id 化归属式下载 + version 非空 + fetch_record）

**Files:** Create `app/routers/distribution.py`、扩 `app/store.py`、`tests/test_distribution.py`;Modify main.py

**Interfaces:**
- `GET /api/distribution/plugins?namespace=&service=`(Bearer=pull token;**小写查询参数 `namespace`/`service`,对应 `n.namespaceCode`/`s.serviceCode`**——跨计划契约,已核 `serviceHub/query.ts`):校验 token 属该 namespace→否则 403;查 active 版本 join(service_plugin_version is_active=True → plugin_version → plugin → service → namespace),返回 **数组** `[{pluginName: plugin.code, version: plugin_version.version, url: PLUGIN_DOWNLOAD_BASE_URL + "/api/distribution/download/"+attachmentId}]`(**这三字段不走 to_camel 改名,与现 `queryPlugin` 字面一致**),写 fetch_record(字段 `namespaceId/pluginId/pluginVersionId/serviceId/fetchDate/remark`,对齐旧 action)。**version 恒非空**(NOT NULL 列保证)。
- `GET /api/distribution/download/{attachment_id}`(Bearer=pull token):反解 token→namespace;校验 `attachment→plugin_version→spv(is_active)→service→namespace == token.namespace`,不符 **404**;`StreamingResponse(storage.open_stream(att.storage_path))`。

- [ ] **Step 1: 失败测试 tests/test_distribution.py(用 `client` fixture;建两个 ns A/B 各发布插件)**
```python
# 1) ns A token 调 plugins?namespace=A&service=... → 200, 返回项含 pluginName/version(非空)/url
# 2) ns A token 调 plugins?namespace=B → 403(token 不属 B)
# 3) ns A token 下载属于 A 的 attachmentId → 200 字节
# 4) ns A token 下载属于 B 的 attachmentId → 404(归属式,防 IDOR)
# 5) plugins 调用后 fetch_record 新增一行
# 6) 兼容形状: 返回是数组[{pluginName,version,url}](sync-plugins 可解析)
# 7) 【评审 H5】不带 Authorization 调 plugins / download → 401/403(无 token 拒绝,纵深回归)
# 8) 【评审 H5】带格式合法但无任何 pull_token_hash 匹配的随机 token → 403/404(token 解析对 None/空/无匹配先拒再相等校验)
```

- [ ] **Step 2: 实现**(pull token 解析:**对 None/空先短路拒绝**,再遍历 namespace 用 `tokens.verify_token` 比对 pull_token_hash;query 带 namespace 再 verify 该 ns。**download 不靠 query namespace**,只靠 token→ns + 归属链。**注意中间件白名单已放行 `/api/distribution/**`,鉴权全在端点内 pull token,务必实现 #7/#8**)
- [ ] **Step 3:** 绿(8 条;尤其 #4 IDOR、#1 version 非空、#7/#8 无 token/坏 token)。 **Step 4: commit** `feat(platform): 分发端点(queryPlugin 兼容 + id 归属式下载防 IDOR + 无token拒绝 + fetch_record)`

---

### Task 11.5: GET /api/fetch-records（获取记录列表,服务端分页,评审 H1）

**Files:** Create `app/routers/fetch_records.py`、扩 `app/store.py`、`tests/test_fetch_records.py`;Modify main.py(include router)

> **评审 H1(已核 `t_fetch_records` 真实表)**:spec 功能对照表明确点名 `fetch_record list → 获取记录 → 手写`,P1-SPA「获取记录」页(7 页之一)依赖此端点。原 P1a 只「写」fetch_record(Task 11)无「读列表」,会让该页 404。

**Interfaces:**
- `GET /api/fetch-records`(`require_session`):**服务端分页**(信封 `{count,rows,page,pageSize,totalPage}`,`page/pageSize` 必备——审计表无界,评审 M2);可选 `?namespaceId=`/`?serviceId=` 过滤;rows 经 LEFT JOIN 带只读 `namespaceCode`/`serviceCode`/`pluginCode`/`version`(评审 H3)。

- [ ] **Step 1: 失败测试(用 `client` fixture)**:写入若干 fetch_record(经 distribution 或直插)后,`GET /api/fetch-records?page=1&pageSize=2` → 信封正确、`len(rows)<=2`、含 `serviceCode`/`pluginCode`/`version`;`?namespaceId=` 过滤生效;无 token → 401。
- [ ] **Step 2: 实现**(复用 store 泛型 list + LEFT JOIN;`response_model` 或 `.model_dump(by_alias=True)` 出 camelCase)。
- [ ] **Step 3:** 绿。 **Step 4: commit** `feat(platform): 获取记录列表端点(服务端分页 + JOIN 名称)`

---

### Task 12: .env.example + Dockerfile + README

**Files:** Create `service-platform/.env.example`、`Dockerfile`、`README.md`

- [ ] **Step 1: .env.example**(列全 env:DATABASE_URL、PLATFORM_ADMIN_USER/PASSWORD、PLATFORM_JWT_SECRET/TTL、SERVICE_HUB_URL、HUB_ADMIN_TOKEN、PLUGIN_STORAGE_DIR、PLUGIN_DOWNLOAD_BASE_URL、HOST/PORT)。
- [ ] **Step 2: Dockerfile**(P1a:`python:3.12-slim` + `pip install -r requirements.txt` + `CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]`;照 service-hub Dockerfile;多阶段含 SPA 留 P1-SPA 计划补)。
- [ ] **Step 3: README.md**(本地起、跑测试、env 说明、与 hub/分发节点的关系一段;**注明上传大小依赖 nginx `client_max_body_size`**——评审 L3)。
- [ ] **Step 4: commit** `docs(platform): .env.example + Dockerfile + README`

---

## Self-Review

**1. Spec 覆盖**:登录(T3)✅、default-deny 中间件守 /api/**(T3.5)✅、台账 CRUD 逐资源(T6a plugin / T6b namespace+service+service_plugin)✅、上传 version=package.json(T8/T9)✅、发布/历史激活/回滚单活+事务(T10)✅、queryPlugin 兼容+id 归属式下载+version 非空+无token拒绝+fetch_record 写(T11)✅、**获取记录列表(T11.5)✅**、per-ns pull token(T5/T7/T11)✅、真 DB 约束 alembic(T2)✅、show-once 密钥/pull token(T7)✅、hub provision/rotate(T4)✅。**P1a 不含**:前端 SPA(P1-SPA 计划)、存量迁移(P1b 计划)、命令下发/滚动/日志/机群总览(P2)、hub 隔离改造(部署任务,跨 service-hub 仓——见「遗留顾虑」)。

**2. 占位扫描**:无 TBD;CRUD 用"范例+字段表+特例"非"similar to";每代码步给完整代码或精确字段表。

**3. 类型一致**:`spv_active_key`/`is_active`/`version_order`/`is_rolled_back` 跨 Task2/10/11 一致;`store.publish/reactivate/rollback` 签名与 T10/T11 一致;`storage.parse_tgz/store_tgz/open_stream` 与 T8/T9/T11 一致;`tokens.new_pull_token/verify_token` 与 T5/T7/T11 一致;`require_session` 与 T3/T3.5/T6 一致;`database` 单例统一 `app.main.database`(T1 落定,store/routers 延迟 import),**无 `app.db.database`、无模块级 `from app.main import database`**。

**4. 评审 finding 落地(本次修订)**:
- **B1**(parse_tgz 根级回退,对齐 sync-plugins.js;fixture 加根级布局)→ T8。
- **B2**(测试地基改 service-hub `client` fixture:tmp_path 文件库 + swap `main_module.database` + dispose;conftest 删 `:memory:` setdefault 误述;写进 T1 验收 + 模板)→ Global Constraints「测试地基」+ T1 Step6/验收门。
- **H1**(新增 `GET /api/fetch-records`)→ T11.5。
- **H5**(穿越对抗 `open_stream('../..')`/绝对路径 + `_sanitize` + 分发无token/坏token 拒绝)→ T8 Step1 / T11 Step1 #7#8。
- **H6**(default-deny 中间件守 /api/**,白名单 login/distribution/health;逐路由 Depends 留作纵深;占位无 Depends 路由仍 401 测试)→ T3.5。
- **H7**(namespace create 测试 `monkeypatch.setattr(hc,'provision_agent',...)`)→ T6b Step2。
- **H8**(frozen Settings 改 `object.__setattr__` 或替换模块 settings 引用;删 `raising=False`)→ T4 Step1 注 + T8 Step1 + `client` fixture。
- **M4**(reactivate/rollback 在"清所有 key"与"置目标 key"间强制 `s.flush()`;新增高 PK active→低 PK 历史回滚用例 #7)→ T10。
- **M9**(拆 Task6→6a[store helper+单例落定+plugin CRUD,无 hub]→6b[namespace 含 hub/show-once+service+service_plugin])。
- **M10**(删模块级 `from app.main import database`,改函数内延迟 `import app.main as main_module`;T1 Interfaces 与 T6 对齐到 `app.main.database`)→ T1 + T6a。
- **L1**(`with_for_update` sqlite no-op,真正闸是 UNIQUE+IntegrityError;措辞校正)→ T10。
- **L2**(database 单例规格直接写进 T1,不留执行期)→ Global Constraints + T1。
- **L3**(upload 请求体大小上限 + parse_tgz 校验 `member.size` 防炸弹)→ T8/T9/T12。
- **Nit-1**(show-once 后重查行断言不含 agentKey 明文)→ T6b/T7。
- **Nit-2**(`jwt.decode(..., options={"require":["sub","exp"]})` + `payload.get('sub')` None→401)→ T3。
- **pin PyJWT**(requirements 全 pin,照 service-hub;空密钥拒绝启动 hardening)→ T1 Step1 + T3。

**5. 跨计划契约(在本计划钉死,SPA/P1b 依赖)**:全 camelCase 模型(`to_camel`+`MODEL_CONFIG`,响应禁手搓 snake)、分发端点保留 `pluginName/version/url`+小写 `namespace`/`service` 查询参数、台账 LEFT JOIN 回可读名(H3)、列表信封 `{count,rows,page,pageSize,totalPage}`+分页(M2,fetch-records 必服务端分页)、releases list filter 语义(H4,不新建聚合端点)、级联过滤 `?namespaceId=`/`?serviceId=`(M3)、`uiSchemas.sql` 文件名(大写 S)——全部写入「跨计划契约」节。

**遗留顾虑(执行/编排前须知)**:
- **hub 隔离(M-3)+ 网段对抗测试**:spec 第 1 号安全控制,P1a 明确**不含**(跨 service-hub 仓部署任务)。须在三计划之外**新增一份极小 P1-部署/隔离子计划**(hub compose 绑 `127.0.0.1` 或共享 internal network + 异网段直连被拒对抗脚本 + 给 hub 三个零鉴权读端点补 `_require_admin_token`),或在编排清单显式登记为 P1 阻塞项并指派执行者;**若归独立批次,须在 spec 把它从 P1 安全/测试验收门正式移走**,而非各计划默认已被别处覆盖。
- **P1-SPA / P1b 先决**:本计划「跨计划契约」节(H1/H2/H3/H4/M2/M3)定稿后,P1-SPA 与 P1b 才可派工;此外 P1-SPA 仍需补 M-7 基线派生任务 + CSP 中间件,P1b 仍需补 M5/M6/M7/M8 与根级布局 smoke——均在各自计划处理,不属 P1a。
