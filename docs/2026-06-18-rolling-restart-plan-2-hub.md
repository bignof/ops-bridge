# 零中断滚动重启 — 实现计划 2 / 3：service-hub

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现。步骤用 `- [ ]` 跟踪。
> 配套设计：`docs/2026-06-18-zero-downtime-rolling-restart-design.md`（v2）。依赖计划 1 的**协议契约**（agent 的 `list-instances` / `graceful-restart` 消息形状）。

**Goal:** 给 service-hub 增加滚动重启编排：`POST /api/rolling-restart` 发现节点（让 agent 查）→ 逐个 `graceful-restart`（按序、健康门、失败即停）→ 进度可查；补齐"按 requestId 等结果"原语、结构化结果通道、rolling-task 落表、并发保护、hub 重启恢复，并补 B1 鉴权。

**Architecture:** 新 `routers/rolling.py`（鉴权端点 + 后台编排协程）；`HubState` 增 `call_agent`/`resolve_pending`（future-by-requestId + 超时）与 rolling-task CRUD（同步 `_xxx_sync` + `asyncio.to_thread`，`active_key` 唯一索引做并发锁）；`_handle_agent_message` 加 `*-result` 分支解 future；新 `RollingTaskModel` + Alembic 迁移；`main.py` 启动扫描中断任务。**不复用 CommandModel**；现有 `update`/`restart`/`dispatch` 仅补鉴权、逻辑不动。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2.0、Alembic、pytest 8.3.5 + `fastapi.testclient`。

## Global Constraints

- 运行测试（cwd = `service-hub/`）：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q`。单文件：`... python -m pytest tests/test_rolling.py -q`。hub 无覆盖率门禁，但新逻辑要有代表性测试（成功 + 失败 + 边界）。
- 鉴权 `_require_admin_token(admin_token)` 是**普通函数手调**（非 Depends）：handler 加 `admin_token: str | None = Header(default=None, alias="X-Admin-Token")` 参数，首行调用它。
- 时间存 UTC（`utc_now()`），出参转东八区（`_as_china_time`）。DB 操作走 `_xxx_sync` + `asyncio.to_thread` 双层。
- 新表必须登记进 `app/db.py` 的 `Database._managed_tables`，否则 `init_schema` legacy 检测会误判。
- rolling 命令的超时/reaper **只作用于 rolling**，不得动 `update`（大镜像 pull 可能很久）。
- 提交中文（conventional 前缀英文）；提交前 `git branch --show-current` 确认在功能分支。

## 依赖契约（来自计划 1，hub 按此发/收）

- 发给 agent：`{type:'list-instances', requestId, serviceName}`；`{type:'graceful-restart', requestId, containerId, healthBaseUrl, settleSec, shutdownTimeoutSec, readyTimeoutSec}`
- agent 回：`{type:'list-instances-result', requestId, status, instances:[{address, containerId, healthy, matched}], error?}`；`{type:'graceful-restart-result', requestId, status, error?}`

## File Structure

- Modify `service-hub/app/config.py` — 加 `rolling_*` Settings 字段。
- Modify `service-hub/app/db.py:15` — `_managed_tables` 加 `"rolling_tasks"`。
- Modify `service-hub/app/db_models.py` — 加 `RollingTaskModel`。
- Create `service-hub/migrations/versions/20260618_0004_rolling_tasks.py` — 建表迁移。
- Modify `service-hub/app/store.py` — `HubState.__init__` 加 `_pending`；增 `call_agent`/`resolve_pending`、rolling CRUD（`create_rolling_task`/`update_rolling_nodes`/`finish_rolling`/`get_rolling_task`/`interrupt_running_rolling`）+ 各 `_xxx_sync`。
- Modify `service-hub/app/api_support.py:226` — `_handle_agent_message` 加 `list-instances-result`/`graceful-restart-result` 分支。
- Modify `service-hub/app/routers/commands.py` — `dispatch_command`/`retry_command` 补鉴权。
- Create `service-hub/app/routers/rolling.py` — 端点 + `_run_rolling` 编排协程。
- Modify `service-hub/app/main.py` — 挂 rolling router + 启动扫描中断任务。
- Tests：`tests/test_config_unit.py`(改)、`tests/test_rolling.py`(新)、`tests/test_api.py`(改：B1 鉴权 + 端点)、`tests/test_db.py`(改：legacy 检测硬编码表清单加 `rolling_tasks`)、`tests/test_main_unit.py`(改：B1 直调用例补 token)。

---

### Task 1: config 加 ROLLING_* 设置

**Files:**
- Modify: `service-hub/app/config.py`
- Test: `service-hub/tests/test_config_unit.py`

**Interfaces:**
- Produces: `settings.rolling_settle_sec`(35) / `rolling_shutdown_timeout`(60) / `rolling_ready_timeout`(180) / `rolling_cmd_timeout`(**480**)，均 int。⚠️ **`rolling_cmd_timeout` 必须 ≥ shutdown60 + docker restart120 + ready180 + settle35 = 395 + 余量**，否则冷启动慢的节点会被 hub 误判超时 failed（spec §5 不变式）。

- [ ] **Step 1: 写失败测试**

`tests/test_config_unit.py` 追加：
```python
def test_rolling_defaults(monkeypatch):
    for k in ("ROLLING_SETTLE_SEC", "ROLLING_SHUTDOWN_TIMEOUT", "ROLLING_READY_TIMEOUT", "ROLLING_CMD_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    import importlib
    import app.config as config
    importlib.reload(config)
    s = config.Settings()
    assert s.rolling_settle_sec == 35
    assert s.rolling_shutdown_timeout == 60
    assert s.rolling_ready_timeout == 180
    assert s.rolling_cmd_timeout == 480
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_config_unit.py::test_rolling_defaults -q`
Expected: FAIL（`AttributeError: ... rolling_settle_sec`）

- [ ] **Step 3: 实现**

`app/config.py` 在 `Settings` 内（`database_url` 行后）追加：
```python
    rolling_settle_sec: int = int(os.getenv("ROLLING_SETTLE_SEC", "35"))
    rolling_shutdown_timeout: int = int(os.getenv("ROLLING_SHUTDOWN_TIMEOUT", "60"))
    rolling_ready_timeout: int = int(os.getenv("ROLLING_READY_TIMEOUT", "180"))
    rolling_cmd_timeout: int = int(os.getenv("ROLLING_CMD_TIMEOUT", "480"))  # 须 ≥ shutdown60+restart120+ready180+settle35=395 + 余量
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_config_unit.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/config.py tests/test_config_unit.py
git commit -m "feat(hub): 增加 rolling-restart 相关超时/settle 配置"
```

---

### Task 2: RollingTaskModel + 迁移

**Files:**
- Modify: `service-hub/app/db_models.py`
- Modify: `service-hub/app/db.py:15`（`_managed_tables`）
- Modify: `service-hub/tests/test_db.py`（既有 legacy 检测用例硬编码了表清单，需同步）
- Create: `service-hub/migrations/versions/20260618_0004_rolling_tasks.py`
- Test: `service-hub/tests/test_rolling.py`（新建）

**Interfaces:**
- Produces: 表 `rolling_tasks`，列：`id`(PK)、`task_id`(uniq)、`agent_id`、`service_name`、`status`、`degraded`(bool)、`active_key`(nullable uniq——running 时=`agent:svc`、终态置 NULL 释放并发锁)、`nodes_json`(Text)、`error`(Text,nullable)、`created_at`/`updated_at`/`finished_at`。

- [ ] **Step 1: 写失败测试**

`tests/test_rolling.py`（新文件）：
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database

def _db(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "t.db"))
    db.init_schema()
    return db

def test_rolling_tasks_table_created(tmp_path):
    db = _db(tmp_path)
    from sqlalchemy import inspect
    names = inspect(db.engine).get_table_names()
    assert "rolling_tasks" in names
    db.engine.dispose()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_rolling.py::test_rolling_tasks_table_created -q`
Expected: FAIL（迁移未建表，`rolling_tasks` 不在 names）

- [ ] **Step 3: 实现 — model**

`app/db_models.py` 追加（照 `CommandModel` 模式，import 补 `Boolean`）：
```python
class RollingTaskModel(Base):
    __tablename__ = "rolling_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    service_name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)        # running/done/failed/interrupted
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    active_key: Mapped[str | None] = mapped_column(String(512), nullable=True, unique=True)
    nodes_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: 实现 — 登记 _managed_tables**

`app/db.py:15` 把 `{"agents", "commands", "command_events"}` 改为：
```python
    _managed_tables = {"agents", "commands", "command_events", "rolling_tasks"}
```

- [ ] **Step 4b: 同步修既有 `tests/test_db.py`（否则 legacy 检测分支翻转致红）**

真实 `app/db.py`：`if "alembic_version" not in existing and _managed_tables <= existing: stamp`，否则 `if _managed_tables & existing: raise RuntimeError`。`tests/test_db.py` 硬编码 Inspector 返回 `["agents","commands","command_events"]` 并断言走 stamp 分支；加 `"rolling_tasks"` 到 `_managed_tables` 后，`{4} <= {3}` 为 False 但 `{4} & {3}` 非空 → 翻到 raise 分支、该用例从 PASS 变 FAIL。改法：把该文件里硬编码的表清单（约 14-15 行 + 相关断言）同步加上 `"rolling_tasks"`（保持走 stamp 分支）。生产无影响（线上库已有 alembic_version）。

- [ ] **Step 5: 实现 — 迁移**

`migrations/versions/20260618_0004_rolling_tasks.py`（照 `20260306_0003_*` 头）：
```python
"""add rolling_tasks table

Revision ID: 20260618_0004
Revises: 20260306_0003
Create Date: 2026-06-18 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260618_0004"
down_revision = "20260306_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rolling_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("service_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active_key", sa.String(length=512), nullable=True),
        sa.Column("nodes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_rolling_tasks_task_id"), "rolling_tasks", ["task_id"], unique=True)
    op.create_index(op.f("ix_rolling_tasks_agent_id"), "rolling_tasks", ["agent_id"])
    op.create_index(op.f("ix_rolling_tasks_service_name"), "rolling_tasks", ["service_name"])
    op.create_index(op.f("ix_rolling_tasks_status"), "rolling_tasks", ["status"])
    op.create_index(op.f("ix_rolling_tasks_active_key"), "rolling_tasks", ["active_key"], unique=True)


def downgrade() -> None:
    op.drop_table("rolling_tasks")
```

- [ ] **Step 6: 跑测试确认通过**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_rolling.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add app/db_models.py app/db.py tests/test_db.py migrations/versions/20260618_0004_rolling_tasks.py tests/test_rolling.py
git commit -m "feat(hub): 增加 rolling_tasks 表与迁移(active_key 唯一索引做并发锁)"
```

---

### Task 3: rolling-task CRUD + 并发锁

**Files:**
- Modify: `service-hub/app/store.py`
- Test: `service-hub/tests/test_rolling.py`

**Interfaces:**
- Produces（`HubState` 方法，全部 async + 内部 `_xxx_sync`）：
  - `create_rolling_task(task_id, agent_id, service_name, force) -> dict`；若同 `(agent_id, service_name)` 已有 running（`active_key` 冲突）抛 `RollingConflict`
  - `update_rolling_nodes(task_id, nodes: list[dict]) -> None`
  - `finish_rolling(task_id, status, *, nodes=None, error=None, degraded=False) -> None`（置 `finished_at`、`active_key=None` 释放锁）
  - `get_rolling_task(task_id) -> dict | None`
  - `interrupt_running_rolling() -> int`（启动恢复用：所有 running→interrupted、释放锁，返回条数）
- 新异常 `class RollingConflict(Exception)`（定义在 store.py）。

- [ ] **Step 1: 写失败测试**

`tests/test_rolling.py` 追加：
```python
import asyncio
import pytest
from app.store import HubState, RollingConflict

def _state(tmp_path):
    return HubState(heartbeat_timeout=90, command_history_limit=200, database=_db(tmp_path))

def test_create_and_get_rolling(tmp_path):
    state = _state(tmp_path)
    t = asyncio.run(state.create_rolling_task("task-1", "agent-a", "memory-share", False))
    assert t["status"] == "running" and t["taskId"] == "task-1"
    got = asyncio.run(state.get_rolling_task("task-1"))
    assert got["agentId"] == "agent-a" and got["serviceName"] == "memory-share"

def test_concurrency_conflict(tmp_path):
    state = _state(tmp_path)
    asyncio.run(state.create_rolling_task("task-1", "agent-a", "svc", False))
    with pytest.raises(RollingConflict):
        asyncio.run(state.create_rolling_task("task-2", "agent-a", "svc", False))

def test_finish_releases_lock(tmp_path):
    state = _state(tmp_path)
    asyncio.run(state.create_rolling_task("task-1", "agent-a", "svc", False))
    asyncio.run(state.finish_rolling("task-1", "done", nodes=[{"address": "x", "status": "done"}]))
    got = asyncio.run(state.get_rolling_task("task-1"))
    assert got["status"] == "done" and got["finishedAt"] is not None
    # 锁已释放：可再起一轮
    asyncio.run(state.create_rolling_task("task-2", "agent-a", "svc", False))

def test_interrupt_running(tmp_path):
    state = _state(tmp_path)
    asyncio.run(state.create_rolling_task("task-1", "agent-a", "svc", False))
    n = asyncio.run(state.interrupt_running_rolling())
    assert n == 1
    assert asyncio.run(state.get_rolling_task("task-1"))["status"] == "interrupted"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_rolling.py -q`
Expected: FAIL（`ImportError: RollingConflict` / 方法不存在）

- [ ] **Step 3: 实现**

`app/store.py`：顶部加 `from app.db_models import RollingTaskModel`（与其它 model import 同处）和 `from sqlalchemy.exc import IntegrityError`、`import json`（若未导入）。加异常 + 方法：
```python
class RollingConflict(Exception):
    """同一 (agent, service) 已有滚动在进行。"""


def _rolling_to_dict(record) -> dict:
    return {
        "taskId": record.task_id,
        "agentId": record.agent_id,
        "serviceName": record.service_name,
        "status": record.status,
        "degraded": record.degraded,
        "nodes": json.loads(record.nodes_json or "[]"),
        "error": record.error,
        "createdAt": _as_china_time(record.created_at),
        "updatedAt": _as_china_time(record.updated_at),
        "finishedAt": _as_china_time(record.finished_at) if record.finished_at else None,
    }
```
在 `HubState` 内加方法（async 包装 + sync 实现，照 `store_command` 模式）：
```python
    async def create_rolling_task(self, task_id, agent_id, service_name, force):
        return await asyncio.to_thread(self._create_rolling_task_sync, task_id, agent_id, service_name, force)

    def _create_rolling_task_sync(self, task_id, agent_id, service_name, force):
        now = utc_now()
        with self.database.session_factory() as session:
            record = RollingTaskModel(
                task_id=task_id, agent_id=agent_id, service_name=service_name,
                status="running", degraded=False,  # degraded 终态由 finish_rolling 据实际结果置;创建恒 False
                active_key=f"{agent_id}:{service_name}",
                nodes_json="[]", created_at=now, updated_at=now)
            session.add(record)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise RollingConflict(f"{agent_id}:{service_name} 已有滚动在进行") from exc
            session.refresh(record)
            return _rolling_to_dict(record)

    async def update_rolling_nodes(self, task_id, nodes):
        await asyncio.to_thread(self._update_rolling_nodes_sync, task_id, nodes)

    def _update_rolling_nodes_sync(self, task_id, nodes):
        with self.database.session_factory() as session:
            record = session.query(RollingTaskModel).filter_by(task_id=task_id).first()
            if record is None:
                return
            record.nodes_json = json.dumps(nodes, ensure_ascii=False)
            record.updated_at = utc_now()
            session.commit()

    async def finish_rolling(self, task_id, status, *, nodes=None, error=None, degraded=False):
        await asyncio.to_thread(self._finish_rolling_sync, task_id, status, nodes, error, degraded)

    def _finish_rolling_sync(self, task_id, status, nodes, error, degraded):
        now = utc_now()
        with self.database.session_factory() as session:
            record = session.query(RollingTaskModel).filter_by(task_id=task_id).first()
            if record is None:
                return
            record.status = status
            record.error = error
            record.active_key = None                 # 释放并发锁
            record.degraded = bool(degraded)         # 以编排实际结果为准(force 但健康≥2 完成时不标 degraded)
            if nodes is not None:
                record.nodes_json = json.dumps(nodes, ensure_ascii=False)
            record.updated_at = now
            record.finished_at = now
            session.commit()

    async def get_rolling_task(self, task_id):
        return await asyncio.to_thread(self._get_rolling_task_sync, task_id)

    def _get_rolling_task_sync(self, task_id):
        with self.database.session_factory() as session:
            record = session.query(RollingTaskModel).filter_by(task_id=task_id).first()
            return _rolling_to_dict(record) if record else None

    async def interrupt_running_rolling(self):
        return await asyncio.to_thread(self._interrupt_running_rolling_sync)

    def _interrupt_running_rolling_sync(self):
        now = utc_now()
        with self.database.session_factory() as session:
            rows = session.query(RollingTaskModel).filter_by(status="running").all()
            for r in rows:
                r.status = "interrupted"
                r.active_key = None
                r.error = "hub 重启,滚动被中断,需人工确认"
                r.updated_at = now
                r.finished_at = now
            session.commit()
            return len(rows)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_rolling.py -q`
Expected: PASS（含并发冲突、释放锁、中断恢复）

- [ ] **Step 5: 提交**

```bash
git add app/store.py tests/test_rolling.py
git commit -m "feat(hub): rolling-task CRUD + active_key 并发锁 + 中断恢复"
```

---

### Task 4: call_agent / resolve_pending（按 requestId 等结果 + 超时）

**Files:**
- Modify: `service-hub/app/store.py`（`HubState.__init__` + 新方法）
- Test: `service-hub/tests/test_rolling.py`

**Interfaces:**
- Produces:
  - `call_agent(agent_id, message: dict, timeout: float) -> dict`：注册 future(键=`message['requestId']`)→`get_connection` 取 WS→`send_json(message)`→`await asyncio.wait_for(future, timeout)`；连接不存在抛 RuntimeError；超时抛 `asyncio.TimeoutError`；finally 清理 future。
  - `resolve_pending(request_id, payload: dict) -> None`：若有等待该 requestId 的 future 且未完成则 `set_result(payload)`。
- Consumes（既有）：`self._connections`、`self._lock`、`get_connection`。

- [ ] **Step 1: 写失败测试**

`tests/test_rolling.py` 追加：
```python
class FakeWS:
    def __init__(self):
        self.sent = []
    async def send_json(self, payload):
        self.sent.append(payload)

def test_call_agent_resolves(tmp_path):
    state = _state(tmp_path)
    ws = FakeWS()
    state._connections["agent-a"] = ws

    async def scenario():
        async def replier():
            # 模拟 agent 回包
            await asyncio.sleep(0)
            await state.resolve_pending("req-1", {"type": "x-result", "requestId": "req-1", "status": "success"})
        task = asyncio.create_task(replier())
        res = await state.call_agent("agent-a", {"type": "x", "requestId": "req-1"}, timeout=5)
        await task
        return res

    res = asyncio.run(scenario())
    assert res["status"] == "success"
    assert ws.sent[0]["requestId"] == "req-1"

def test_call_agent_timeout(tmp_path):
    state = _state(tmp_path)
    state._connections["agent-a"] = FakeWS()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(state.call_agent("agent-a", {"type": "x", "requestId": "req-x"}, timeout=0.05))

def test_call_agent_no_connection(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(state.call_agent("missing", {"type": "x", "requestId": "r"}, timeout=1))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_rolling.py -k call_agent -q`
Expected: FAIL（方法不存在）

- [ ] **Step 3: 实现**

`HubState.__init__` 末尾加：
```python
        self._pending: dict[str, asyncio.Future] = {}
```
加方法：
```python
    async def call_agent(self, agent_id, message, timeout):
        request_id = message["requestId"]
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        async with self._lock:
            self._pending[request_id] = future
        try:
            websocket = self._connections.get(agent_id)
            if websocket is None:
                raise RuntimeError(f"agent {agent_id} 连接不可用")
            await websocket.send_json(message)
            return await asyncio.wait_for(future, timeout)
        finally:
            async with self._lock:
                self._pending.pop(request_id, None)

    async def resolve_pending(self, request_id, payload):
        async with self._lock:
            future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(payload)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_rolling.py -k call_agent -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add app/store.py tests/test_rolling.py
git commit -m "feat(hub): 增加 call_agent/resolve_pending(按 requestId 等结果+超时)"
```

---

### Task 5: _handle_agent_message 解 future（结构化结果通道）

**Files:**
- Modify: `service-hub/app/api_support.py`（`_handle_agent_message`）
- Test: `service-hub/tests/test_rolling.py`

**Interfaces:**
- `_handle_agent_message(agent_id, payload)`：对 `payload['type'] in ('list-instances-result','graceful-restart-result')` → `await hub_state.resolve_pending(payload['requestId'], payload)`；现有 `ack`/`result`/`logs_*` 分支不动。

- [ ] **Step 1: 写失败测试**

`tests/test_rolling.py` 追加：
```python
def test_handle_agent_message_resolves_rolling(tmp_path, monkeypatch):
    import app.main as main_module
    import app.api_support as api_support
    state = _state(tmp_path)
    monkeypatch.setattr(main_module, "hub_state", state)
    ws = FakeWS()
    state._connections["agent-a"] = ws

    async def scenario():
        async def feeder():
            await asyncio.sleep(0)
            await api_support._handle_agent_message("agent-a", {
                "type": "list-instances-result", "requestId": "req-1",
                "status": "success", "instances": []})
        task = asyncio.create_task(feeder())
        res = await state.call_agent("agent-a", {"type": "list-instances", "requestId": "req-1"}, timeout=5)
        await task
        return res

    res = asyncio.run(scenario())
    assert res["type"] == "list-instances-result" and res["status"] == "success"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_rolling.py::test_handle_agent_message_resolves_rolling -q`
Expected: FAIL（future 不被解，wait_for 超时）

- [ ] **Step 3: 实现**

`app/api_support.py` 的 `_handle_agent_message` 内，在 `result` 分支之后、`logs_*` 分支附近，加：
```python
    if msg_type in ("list-instances-result", "graceful-restart-result"):
        request_id = payload.get("requestId")
        if request_id:
            await main_module.hub_state.resolve_pending(request_id, payload)
        return
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_rolling.py::test_handle_agent_message_resolves_rolling -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/api_support.py tests/test_rolling.py
git commit -m "feat(hub): _handle_agent_message 解 rolling 结果 future"
```

---

### Task 6: B1 — dispatch/retry 补鉴权（含改既有测试）

**Files:**
- Modify: `service-hub/app/routers/commands.py`（`dispatch_command`、`retry_command`）
- Modify: `service-hub/tests/test_api.py`（既有不带 token 的 dispatch/retry 用例补 token + 新增 403 用例）
- Modify: `service-hub/tests/test_main_unit.py`（**直调** dispatch/retry 的用例补 token——B-4，最易漏）
- Test: `service-hub/tests/test_api.py`、`tests/test_main_unit.py`

**Interfaces:**
- `dispatch_command`/`retry_command` 新增 `admin_token: str | None = Header(default=None, alias="X-Admin-Token")`，首行 `_require_admin_token(admin_token)`。

- [ ] **Step 1: 写失败测试（新增 403 断言）**

`tests/test_api.py` 追加（**不带 token 应先被鉴权拦下，无需在线 agent——403 早于 agent 查找；无 `_attach_test_agent` 这种 helper**）：
```python
def test_dispatch_requires_admin_token(client):
    resp = client.post("/api/agents/agent-a/commands",
                       json={"requestId": "r1", "action": "restart", "dir": "/srv/a"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid admin token"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_api.py::test_dispatch_requires_admin_token -q`
Expected: FAIL（现状无鉴权，返回 202/409 而非 403）

- [ ] **Step 3: 实现 — 补鉴权**

`app/routers/commands.py`：
- `dispatch_command` 签名加参数：
  ```python
  async def dispatch_command(
      request: CommandDispatchRequest,
      agent_id: str = Path(...),
      admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
      requested_by: str | None = Header(default=None, alias="X-Requested-By"),
      request_source: str | None = Header(default=None, alias="X-Requested-Source"),
  ) -> CommandDispatchResponse:
      import app.main as main_module
      from app.api_support import _require_admin_token
      _require_admin_token(admin_token)
      agent = await main_module.hub_state.get_agent(agent_id)
      ...
  ```
  （若文件顶部已 import `_require_admin_token` 则直接用。）
- `retry_command` 同样加 `admin_token` Header 参数 + 首行 `_require_admin_token(admin_token)`。

- [ ] **Step 4: 修既有测试（回归）—— test_api.py**

把 `tests/test_api.py` 中 **POST** `/api/agents/{id}/commands`（约 226/258/276 行，3 处）与 **POST** `/api/commands/{id}/retry`（约 447/474/481 行，3 处）的请求**统一加** `headers={"X-Admin-Token": "test-admin-token"}`。例如：
```python
resp = client.post("/api/agents/agent-a/commands",
                   json={"requestId": "r1", "action": "restart", "dir": "/srv/a"},
                   headers={"X-Admin-Token": "test-admin-token"})
```
定位用 `/commands"` 与 `/retry"`（注意 `:/commands"` 匹配不到——真实 URL 形如 `/api/agents/agent-a/commands`）。GET `/api/commands` 列表不受影响；body 422 用例（约 258 行）因 pydantic 校验先于鉴权、仍是 422、无需改。

- [ ] **Step 4b: 修既有测试（回归）—— test_main_unit.py（B-4，最易漏）**

`tests/test_main_unit.py` 把 handler 当协程**直调且不传 token**（`test_dispatch_command_*` / `test_retry_command_*`，约 305-409 行），B1 后首行鉴权先抛 403 → 全红。逐个改：
- 所有 `dispatch_command(request=..., agent_id="agent-a")` 加 `admin_token="test-admin-token"`；
- 所有 `retry_command("req-1", ...)`（现位置传 request_id）改为 `retry_command("req-1", admin_token="test-admin-token")`；
- 补 1 个 `admin_token=None → 403`（`pytest.raises(HTTPException)` 断 status_code==403）直调用例。

- [ ] **Step 5: 跑全量 hub 测试确认通过**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q`
Expected: PASS（既有用例带 token 通过 + 新 401 用例通过）

- [ ] **Step 6: 提交**

```bash
git add app/routers/commands.py tests/test_api.py tests/test_main_unit.py
git commit -m "fix(hub): dispatch/retry 命令补 admin token 鉴权(B1)+同步既有测试(test_api/test_main_unit)"
```

---

### Task 7: rolling 端点 + 编排协程

**Files:**
- Create: `service-hub/app/routers/rolling.py`
- Test: `service-hub/tests/test_rolling.py`（编排逻辑，FakeHubState）+ `tests/test_api.py`（端点鉴权/taskId）

**Interfaces:**
- `_run_rolling(task_id, agent_id, service_name, force, hub_state, settings) -> None`（编排：list→对不上号即停→健康<2 且非 force 即拒→逐个 graceful-restart 失败即停→`finish_rolling`）
- `POST /api/rolling-restart`（鉴权）入参 `{agentId, serviceName, force?}`→`create_rolling_task`(冲突 409)→`asyncio.create_task(_run_rolling(...))`→`{taskId}`
- `GET /api/rolling-restart/{task_id}`（鉴权）→ `get_rolling_task`（404 若无）

- [ ] **Step 1: 写失败测试（编排逻辑，注入 FakeHubState）**

`tests/test_rolling.py` 追加：
```python
from app.routers import rolling as rolling_router

class FakeSettings:
    rolling_settle_sec = 1
    rolling_shutdown_timeout = 60
    rolling_ready_timeout = 10
    rolling_cmd_timeout = 30

class FakeHubState:
    def __init__(self, scripted):
        self.scripted = scripted        # list of dicts to return from call_agent in order
        self.calls = []
        self.node_updates = []
        self.finished = None
    async def call_agent(self, agent_id, message, timeout):
        self.calls.append(message)
        return self.scripted.pop(0)
    async def update_rolling_nodes(self, task_id, nodes):
        self.node_updates.append([dict(n) for n in nodes])
    async def finish_rolling(self, task_id, status, *, nodes=None, error=None, degraded=False):
        self.finished = {"status": status, "nodes": nodes, "error": error, "degraded": degraded}

def _inst(addr, cid, matched=True, healthy=True):
    return {"address": addr, "containerId": cid, "healthy": healthy, "matched": matched}

def test_run_rolling_happy_path():
    hub = FakeHubState([
        {"status": "success", "instances": [_inst("h:18029", "a"), _inst("h:18030", "b")]},
        {"status": "success"},  # graceful-restart node a
        {"status": "success"},  # graceful-restart node b
    ])
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", False, hub, FakeSettings()))
    assert hub.finished["status"] == "done"
    # 两次 graceful-restart 按序
    gr = [c for c in hub.calls if c["type"] == "graceful-restart"]
    assert [c["containerId"] for c in gr] == ["a", "b"]
    assert gr[0]["healthBaseUrl"] == "http://h:18029"

def test_run_rolling_unmatched_aborts():
    hub = FakeHubState([{"status": "success", "instances": [_inst("h:1", None, matched=False)]}])
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", False, hub, FakeSettings()))
    assert hub.finished["status"] == "failed" and "对不上号" in hub.finished["error"]
    assert not any(c["type"] == "graceful-restart" for c in hub.calls)

def test_run_rolling_single_instance_rejected():
    hub = FakeHubState([{"status": "success", "instances": [_inst("h:1", "a")]}])
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", False, hub, FakeSettings()))
    assert hub.finished["status"] == "failed" and "健康实例" in hub.finished["error"]

def test_run_rolling_single_instance_force_degraded():
    hub = FakeHubState([
        {"status": "success", "instances": [_inst("h:1", "a")]},
        {"status": "success"},
    ])
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", True, hub, FakeSettings()))
    assert hub.finished["status"] == "degraded"

def test_run_rolling_fail_stop():
    hub = FakeHubState([
        {"status": "success", "instances": [_inst("h:1", "a"), _inst("h:2", "b")]},
        {"status": "failed", "error": "boom"},   # node a fails
    ])
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", False, hub, FakeSettings()))
    assert hub.finished["status"] == "failed"
    gr = [c for c in hub.calls if c["type"] == "graceful-restart"]
    assert len(gr) == 1   # 失败即停,不发第二个
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_rolling.py -k run_rolling -q`
Expected: FAIL（无 `app.routers.rolling`）

- [ ] **Step 3: 实现**

`app/routers/rolling.py`：
```python
import asyncio
import uuid

from fastapi import APIRouter, Header, HTTPException, Path, status
from pydantic import BaseModel

from app.api_support import _require_admin_token
from app.store import RollingConflict

router = APIRouter(tags=["滚动重启"])

_background: set[asyncio.Task] = set()


class RollingRestartRequest(BaseModel):
    agentId: str
    serviceName: str
    force: bool = False


async def _run_rolling(task_id, agent_id, service_name, force, hub_state, settings):
    try:
        req = str(uuid.uuid4())
        listed = await hub_state.call_agent(
            agent_id, {"type": "list-instances", "requestId": req, "serviceName": service_name},
            timeout=settings.rolling_cmd_timeout)
        if listed.get("status") != "success":
            await hub_state.finish_rolling(task_id, "failed", error=f"list-instances 失败: {listed.get('error')}")
            return
        instances = listed.get("instances") or []
        unmatched = [i["address"] for i in instances if not i.get("matched")]
        if unmatched:
            await hub_state.finish_rolling(task_id, "failed", error=f"实例对不上号(可能非本机或匹配键错): {unmatched}")
            return
        healthy = [i for i in instances if i.get("healthy")]
        if len(healthy) < 2 and not force:
            await hub_state.finish_rolling(task_id, "failed",
                error=f"健康实例数={len(healthy)}<2,无法零中断滚动;请扩容或 force")
            return
        nodes = [{"address": i["address"], "containerId": i["containerId"], "status": "pending"} for i in healthy]
        await hub_state.update_rolling_nodes(task_id, nodes)
        for idx, node in enumerate(nodes):
            nodes[idx]["status"] = "in-progress"
            await hub_state.update_rolling_nodes(task_id, nodes)
            req = str(uuid.uuid4())
            res = await hub_state.call_agent(agent_id, {
                "type": "graceful-restart", "requestId": req,
                "containerId": node["containerId"],
                "healthBaseUrl": f"http://{node['address']}",
                "settleSec": settings.rolling_settle_sec,
                "shutdownTimeoutSec": settings.rolling_shutdown_timeout,
                "readyTimeoutSec": settings.rolling_ready_timeout,
            }, timeout=settings.rolling_cmd_timeout)
            if res.get("status") != "success":
                nodes[idx]["status"] = "failed"
                nodes[idx]["error"] = res.get("error")
                await hub_state.finish_rolling(task_id, "failed", nodes=nodes,
                    error=f"节点 {node['address']} 失败,停止滚动")
                return
            nodes[idx]["status"] = "done"
            await hub_state.update_rolling_nodes(task_id, nodes)
        await hub_state.finish_rolling(task_id, "degraded" if (len(healthy) < 2) else "done",
                                       nodes=nodes, degraded=(len(healthy) < 2))
    except asyncio.TimeoutError:
        await hub_state.finish_rolling(task_id, "failed", error="等待 agent 命令结果超时")
    except Exception as exc:  # noqa: BLE001
        await hub_state.finish_rolling(task_id, "failed", error=str(exc))


@router.post("/api/rolling-restart")
async def rolling_restart(
    request: RollingRestartRequest,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
):
    _require_admin_token(admin_token)
    import app.main as main_module
    task_id = str(uuid.uuid4())
    try:
        await main_module.hub_state.create_rolling_task(
            task_id, request.agentId, request.serviceName, request.force)
    except RollingConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    task = asyncio.create_task(
        _run_rolling(task_id, request.agentId, request.serviceName, request.force,
                     main_module.hub_state, main_module.settings))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"taskId": task_id}


@router.get("/api/rolling-restart/{task_id}")
async def get_rolling(
    task_id: str = Path(...),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
):
    _require_admin_token(admin_token)
    import app.main as main_module
    task = await main_module.hub_state.get_rolling_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task 不存在")
    return task
```

- [ ] **Step 4: 跑编排逻辑测试确认通过**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_rolling.py -k run_rolling -q`
Expected: PASS（5 passed）

> 注：端点鉴权/taskId 的 HTTP 测试**移到 Task 8**（router 在 Task 8 才 `include_router`，本任务阶段访问会得 404 红用例、违反"提交即绿"）。本任务只交付 `rolling.py` + `_run_rolling` 逻辑测试（FakeHubState）。

- [ ] **Step 5: 提交**

```bash
git add app/routers/rolling.py tests/test_rolling.py
git commit -m "feat(hub): rolling-restart 端点 + 编排协程(健康门/按序/失败即停)"
```

---

### Task 8: main.py 挂 router + 启动恢复

**Files:**
- Modify: `service-hub/app/main.py`（include_router + lifespan 内 `interrupt_running_rolling`）
- Test: `service-hub/tests/test_api.py`

**Interfaces:**
- 启动时（lifespan，在 `init_schema` 之后）调 `await hub_state.interrupt_running_rolling()` 把残留 running 标 interrupted。

- [ ] **Step 1: 写失败测试（router 挂上后这三个应可通；现状 404）**

`tests/test_api.py` 追加（含从 Task 7 移来的端点契约测试）：
```python
def test_rolling_router_mounted(client):
    # 未带 token 返回 403 而非 404,证明路由已注册
    resp = client.post("/api/rolling-restart", json={"agentId": "a", "serviceName": "s"})
    assert resp.status_code == 403

def test_rolling_restart_returns_task_id(client):
    resp = client.post("/api/rolling-restart", json={"agentId": "a", "serviceName": "s"},
                       headers={"X-Admin-Token": "test-admin-token"})
    assert resp.status_code == 200
    assert "taskId" in resp.json()
    # 后台编排会因无在线 agent 很快 finish,此处只验端点契约

def test_rolling_status_404_unknown(client):
    resp = client.get("/api/rolling-restart/nope", headers={"X-Admin-Token": "test-admin-token"})
    assert resp.status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_api.py::test_rolling_router_mounted -q`
Expected: FAIL（404，router 未挂）

- [ ] **Step 3: 实现**

`app/main.py`：
- import：`from app.routers.rolling import router as rolling_router`
- 在其它 `app.include_router(...)` 处加：`app.include_router(rolling_router)`
- 在 lifespan 启动段（`database.init_schema()` 之后、`hub_state` 已建好处）加：
  ```python
  interrupted = await hub_state.interrupt_running_rolling()
  if interrupted:
      logger.warning("启动恢复:发现 %s 个中断的滚动任务,已标记 interrupted", interrupted)
  ```
  （`logger` 用本模块既有 logger；若无则 `import logging; logger = logging.getLogger(__name__)`。）

- [ ] **Step 4: 跑全量 hub 测试确认通过**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q`
Expected: PASS（含 Task 7 端点测试 + 本任务 + 既有全绿）

- [ ] **Step 5: 提交**

```bash
git add app/main.py tests/test_api.py
git commit -m "feat(hub): 挂 rolling router + 启动扫描中断滚动任务"
```

---

## Self-Review（已核对）

- **Spec 覆盖**：rolling 端点 + 鉴权（§4.3, B1）✅；发现交给 agent、hub 不碰 nacos（§2/§3）✅；等结果 future + 超时（H4）✅；结构化结果通道（H4，`*-result` 分支）✅；rolling-task 落表 + active_key 并发锁（H4/M3）✅；最小存活保护 + force/degraded（H3）✅；失败即停（§7）✅；hub 重启恢复（M3）✅；不复用 CommandModel（H4）✅；B1 含改既有测试（向后兼容 §4.5）✅。
- **类型/命名一致**：`call_agent(agent_id, message, timeout)`、`resolve_pending(request_id, payload)`、`create_rolling_task/update_rolling_nodes/finish_rolling/get_rolling_task/interrupt_running_rolling`、`RollingConflict`、`_run_rolling(...,hub_state,settings)` 在任务间一致；node dict 形 `{address,containerId,status,error?}`；结果 type `list-instances-result`/`graceful-restart-result` 与计划 1 契约一致。
- **占位符**：无。
- **未决（P1.5，已与 spec §7 对齐）**：进度为粗粒度（pending/in-progress/done/failed，hub 驱动）；细粒度 draining/restarting/ready 由 agent 发进度消息属 P1.5。WS 断连：**P1 靠 `rolling_cmd_timeout` 超时兜底判 failed**；"标 `unknown` 并即时停"属 P1.5（spec §7 已标 P1.5，三处一致）。

## 后续增强（不阻塞 P1，登记备查）
- M4 断连补发：agent 长命令期间 WS 抖动→按 requestId 幂等补发；hub 侧 disconnect 时把在途 rolling future 标 unknown 并停（当前靠 `rolling_cmd_timeout` 超时兜底）。
- 细粒度进度：agent 发 `graceful-restart-progress`（draining/restarting/ready）→ hub 更新 node 子状态。
