import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import pytest

from app.db import Database
from app.store import HubState, RollingConflict

def _db(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "t.db"))
    db.init_schema()
    return db

def _state(tmp_path):
    return HubState(heartbeat_timeout=90, command_history_limit=200, database=_db(tmp_path))

def test_rolling_tasks_table_created(tmp_path):
    db = _db(tmp_path)
    from sqlalchemy import inspect
    names = inspect(db.engine).get_table_names()
    assert "rolling_tasks" in names
    db.engine.dispose()

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
