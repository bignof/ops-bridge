import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import pytest

from app.db import Database
from app.hub.store import HubState, RollingConflict

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


def test_update_rolling_nodes_roundtrip_preserves_chinese(tmp_path):
    # L1: 真实 store 往返 —— 中文字段值不被转义(ensure_ascii=False 生效)
    state = _state(tmp_path)
    asyncio.run(state.create_rolling_task("task-1", "agent-a", "svc", False))
    nodes = [
        {"address": "h:18029", "containerId": "a", "status": "进行中", "phase": "in-progress"},
        {"address": "h:18030", "containerId": "b", "status": "等待中", "error": "节点未就绪"},
    ]
    asyncio.run(state.update_rolling_nodes("task-1", nodes))
    got = asyncio.run(state.get_rolling_task("task-1"))
    assert got["nodes"] == nodes  # 往返一致,中文原样
    assert got["nodes"][0]["status"] == "进行中"
    assert got["nodes"][1]["error"] == "节点未就绪"


def test_update_rolling_nodes_unknown_task_is_noop(tmp_path):
    # L1: 对不存在 task 调用不抛
    state = _state(tmp_path)
    asyncio.run(state.update_rolling_nodes("nope", [{"address": "x", "status": "done"}]))
    assert asyncio.run(state.get_rolling_task("nope")) is None


def test_finish_rolling_degraded_bool_roundtrip(tmp_path):
    # L2: degraded 终态 bool 真实 DB 往返
    state = _state(tmp_path)
    asyncio.run(state.create_rolling_task("task-d", "agent-a", "svc-d", True))
    asyncio.run(state.finish_rolling("task-d", "degraded", degraded=True))
    assert asyncio.run(state.get_rolling_task("task-d"))["degraded"] is True

    asyncio.run(state.create_rolling_task("task-n", "agent-b", "svc-n", False))
    asyncio.run(state.finish_rolling("task-n", "done", degraded=False))
    assert asyncio.run(state.get_rolling_task("task-n"))["degraded"] is False

def test_interrupt_running(tmp_path):
    state = _state(tmp_path)
    asyncio.run(state.create_rolling_task("task-1", "agent-a", "svc", False))
    n = asyncio.run(state.interrupt_running_rolling())
    assert n == 1
    assert asyncio.run(state.get_rolling_task("task-1"))["status"] == "interrupted"


def test_interrupt_keeps_lock_until_acknowledge(tmp_path):
    # M2: 重启恢复标 interrupted 但不释放 active_key —— 同 (agent,service) 新滚动仍被拦,
    # 直到 acknowledge_rolling 人工确认释放锁后才能再起一轮。
    state = _state(tmp_path)
    asyncio.run(state.create_rolling_task("task-1", "agent-a", "svc", False))
    asyncio.run(state.interrupt_running_rolling())
    assert asyncio.run(state.get_rolling_task("task-1"))["status"] == "interrupted"

    # 锁仍在:同 key 新滚动被唯一约束挡住
    with pytest.raises(RollingConflict):
        asyncio.run(state.create_rolling_task("task-2", "agent-a", "svc", False))

    # 人工确认释放锁
    assert asyncio.run(state.acknowledge_rolling("task-1")) is True
    # 释放后可再起一轮
    asyncio.run(state.create_rolling_task("task-2", "agent-a", "svc", False))
    assert asyncio.run(state.get_rolling_task("task-2"))["status"] == "running"


def test_acknowledge_non_interrupted_is_noop(tmp_path):
    # 非 interrupted(如 running)调 acknowledge 返回 False 且不动锁
    state = _state(tmp_path)
    asyncio.run(state.create_rolling_task("task-1", "agent-a", "svc", False))
    assert asyncio.run(state.acknowledge_rolling("task-1")) is False
    # 锁未被错误释放:同 key 仍冲突
    with pytest.raises(RollingConflict):
        asyncio.run(state.create_rolling_task("task-2", "agent-a", "svc", False))


def test_acknowledge_unknown_task_returns_false(tmp_path):
    state = _state(tmp_path)
    assert asyncio.run(state.acknowledge_rolling("nope")) is False

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
    import app.hub.api_support as api_support
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


from app.hub.routers import rolling as rolling_router

class FakeSettings:
    rolling_settle_sec = 1
    rolling_shutdown_timeout = 60
    rolling_ready_timeout = 10
    rolling_cmd_timeout = 30

class FakeHubState:
    def __init__(self, scripted, *, call_side_effect=None):
        self.scripted = scripted        # list of dicts to return from call_agent in order
        # call_side_effect: 可选异常(类/实例),被调用时直接抛出,用于覆盖超时/通用异常出口
        self.call_side_effect = call_side_effect
        self.calls = []
        self.node_updates = []
        self.finished = None
    async def call_agent(self, agent_id, message, timeout):
        self.calls.append(message)
        if self.call_side_effect is not None:
            raise self.call_side_effect
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
    assert hub.finished["degraded"] is False  # L2: 健康≥2 正常完成不标 degraded
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


# === M1: 空实例集 / 全 unhealthy 不得被 force 报成 degraded 软成功 ===

def test_run_rolling_empty_instances_with_force_fails():
    # nacos 未发现任何实例:即便 force=True 也必须 failed,且不发任何 graceful-restart
    hub = FakeHubState([{"status": "success", "instances": []}])
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", True, hub, FakeSettings()))
    assert hub.finished["status"] == "failed"
    assert "无健康实例" in hub.finished["error"]
    assert not any(c["type"] == "graceful-restart" for c in hub.calls)


def test_run_rolling_empty_instances_without_force_fails():
    hub = FakeHubState([{"status": "success", "instances": []}])
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", False, hub, FakeSettings()))
    assert hub.finished["status"] == "failed"
    assert "无健康实例" in hub.finished["error"]
    assert not any(c["type"] == "graceful-restart" for c in hub.calls)


def test_run_rolling_all_unhealthy_with_force_fails():
    # 实例非空但全部 healthy:False —— force 也不放行 healthy==0
    hub = FakeHubState([
        {"status": "success", "instances": [_inst("h:1", "a", healthy=False), _inst("h:2", "b", healthy=False)]},
    ])
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", True, hub, FakeSettings()))
    assert hub.finished["status"] == "failed"
    assert "无健康实例" in hub.finished["error"]
    assert not any(c["type"] == "graceful-restart" for c in hub.calls)


# === M4: timeout / list 失败 / 通用异常 终态出口回归 ===

def test_run_rolling_timeout_yields_failed():
    # call_agent 抛 asyncio.TimeoutError(P1 唯一断连兜底)→ failed 且 error 含"超时"
    hub = FakeHubState([], call_side_effect=asyncio.TimeoutError())
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", False, hub, FakeSettings()))
    assert hub.finished["status"] == "failed"
    assert "超时" in hub.finished["error"]


def test_run_rolling_list_instances_failed_yields_failed():
    # list-instances 返回 status != success → failed 且 error 含 list 失败信息
    hub = FakeHubState([{"status": "failed", "error": "nacos down"}])
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", False, hub, FakeSettings()))
    assert hub.finished["status"] == "failed"
    assert "list-instances" in hub.finished["error"]
    assert "nacos down" in hub.finished["error"]


def test_run_rolling_generic_exception_yields_failed():
    # call_agent 抛普通 Exception → 通用兜底出口 failed
    hub = FakeHubState([], call_side_effect=RuntimeError("agent 连接不可用"))
    asyncio.run(rolling_router._run_rolling("t1", "agent-a", "svc", False, hub, FakeSettings()))
    assert hub.finished["status"] == "failed"
    assert "agent 连接不可用" in hub.finished["error"]


# === M3: 后台任务异常被记录,且正常完成的任务从 _background 移除 ===

def test_on_task_done_logs_exception(caplog):
    # 后台任务抛出异常时,done-callback 必须读取并 logger.error 记录(否则零日志)
    async def boom():
        raise RuntimeError("agent 断连")

    async def scenario():
        task = asyncio.create_task(boom())
        rolling_router._background.add(task)
        try:
            await task
        except RuntimeError:
            pass
        return task

    with caplog.at_level("ERROR", logger="app.hub.routers.rolling"):
        task = asyncio.run(scenario())
        rolling_router._on_task_done(task)

    assert task not in rolling_router._background  # 已从台账移除
    assert any("rolling 后台任务异常" in r.message for r in caplog.records)
    assert any("agent 断连" in (r.exc_text or "") for r in caplog.records if r.exc_info)


def test_on_task_done_clean_completion_no_error(caplog):
    # 正常完成的任务:不记录 ERROR,仍从 _background 移除
    async def ok():
        return None

    async def scenario():
        task = asyncio.create_task(ok())
        rolling_router._background.add(task)
        await task
        return task

    with caplog.at_level("ERROR", logger="app.hub.routers.rolling"):
        task = asyncio.run(scenario())
        rolling_router._on_task_done(task)

    assert task not in rolling_router._background
    assert not any("rolling 后台任务异常" in r.message for r in caplog.records)
