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
    # 终态所有节点都 done
    assert [n["status"] for n in hub.finished["nodes"]] == ["done", "done"]
    # 状态机推进经过 in-progress(任一 node_updates 快照里出现过某节点 in-progress)
    assert any(
        any(n["status"] == "in-progress" for n in snap)
        for snap in hub.node_updates
    )

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
    assert hub.finished["degraded"] is True

def test_run_rolling_fail_stop():
    hub = FakeHubState([
        {"status": "success", "instances": [_inst("h:1", "a"), _inst("h:2", "b")]},
        {"status": "failed", "error": "boom"},   # node a fails
    ])
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", False, hub, FakeSettings()))
    assert hub.finished["status"] == "failed"
    gr = [c for c in hub.calls if c["type"] == "graceful-restart"]
    assert len(gr) == 1   # 失败即停,不发第二个
    # 失败节点标 failed 并带原始 error;后续节点标 skipped(便于区分"未动过"与"被中止跳过")
    assert hub.finished["nodes"][0]["status"] == "failed"
    assert hub.finished["nodes"][0]["error"] == "boom"
    assert hub.finished["nodes"][1]["status"] == "skipped"
