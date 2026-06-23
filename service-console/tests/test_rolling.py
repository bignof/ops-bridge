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

def test_cross_agent_lock_by_service_name(tmp_path):
    # 跨 agent 滚动锁键 = service_name(agent_id 用哨兵 "*"):同 serviceName 并发 → RollingConflict。
    state = _state(tmp_path)
    asyncio.run(state.create_rolling_task("t-1", "*", "svc", False, active_key="svc"))
    # 同服务再起一轮跨机滚动(哪怕换个 task_id / 哨兵)→ 撞 service_name 锁。
    with pytest.raises(RollingConflict):
        asyncio.run(state.create_rolling_task("t-2", "*", "svc", False, active_key="svc"))
    # 释放后可再起。
    asyncio.run(state.finish_rolling("t-1", "done"))
    asyncio.run(state.create_rolling_task("t-2", "*", "svc", False, active_key="svc"))


def test_cross_agent_lock_distinct_from_single_agent_key(tmp_path):
    # 跨机锁键(service_name)与单 agent 锁键(agent:service)正交:互不阻塞。
    state = _state(tmp_path)
    asyncio.run(state.create_rolling_task("t-single", "agent-a", "svc", False))  # 锁 "agent-a:svc"
    # 跨机滚同名服务锁 "svc",不撞单 agent 锁,可并存。
    asyncio.run(state.create_rolling_task("t-cross", "*", "svc", False, active_key="svc"))
    assert asyncio.run(state.get_rolling_task("t-single"))["status"] == "running"
    assert asyncio.run(state.get_rolling_task("t-cross"))["status"] == "running"


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


# =====================================================================================
# P4-1:跨 agent(跨机)顺序滚动协调器 _run_service_rolling
# =====================================================================================
#
# 把承载同一 nacos 服务的各 agent 的 matched-local healthy 实例汇成一条全局有序列表,全局一次
# 只滚一个(逐 graceful-restart);unmatched 过滤丢弃(不再 abort);集群健康门 ≥2 跨 agent 合计;
# 失败即停(freeze,不回滚)。下方测试照 FakeHubState 的 async 桩风格,另立记录 agent_id 的桩。


class _FakeDN:
    """聚合返回的发现行替身:协调器只读 .agent_id 用于定位承载该服务的 agent(稳定序去重)。"""

    def __init__(self, agent_id):
        self.agent_id = agent_id


class FakeServiceHubState:
    """跨 agent 协调器专用桩:按 (agent_id → 该 agent 的 list-instances 结果) 脚本化回包。

    - list_results:{agent_id: {"status": ..., "instances": [...]}};按 agent_id 取该 agent 的
      list-instances 回包(协调器逐 agent 调一次)。
    - graceful_results:list,按调用序逐个 pop 返回 graceful-restart 回包(全局逐一滚)。
    - call_side_effect:可选异常,call_agent 被调时直接抛(覆盖超时/通用异常出口)。
    记录 self.calls = [(agent_id, message)...] 供断言调用次数 + 顺序(含跨 agent 序)。
    """

    def __init__(self, list_results=None, graceful_results=None, *, call_side_effect=None):
        self.list_results = list_results or {}
        self.graceful_results = list(graceful_results or [])
        self.call_side_effect = call_side_effect
        self.calls = []
        self.node_updates = []
        self.finished = None

    async def call_agent(self, agent_id, message, timeout):
        self.calls.append((agent_id, message))
        if self.call_side_effect is not None:
            raise self.call_side_effect
        if message["type"] == "list-instances":
            return self.list_results[agent_id]
        if message["type"] == "graceful-restart":
            return self.graceful_results.pop(0)
        raise AssertionError(f"未预期的命令类型: {message['type']}")

    async def update_rolling_nodes(self, task_id, nodes):
        self.node_updates.append([dict(n) for n in nodes])

    async def finish_rolling(self, task_id, status, *, nodes=None, error=None, degraded=False):
        self.finished = {"status": status, "nodes": nodes, "error": error, "degraded": degraded}


def _patch_agg(monkeypatch, grouped):
    """打桩 app.store.aggregate_discovered_by_nacos —— 协调器经 asyncio.to_thread 调它定位 agents。

    grouped:{nacosService: [agent_id, ...]} → 转成 {nacosService: [_FakeDN(agent_id)...]}。
    """
    import app.store as store

    fake = {svc: [_FakeDN(a) for a in agents] for svc, agents in grouped.items()}
    monkeypatch.setattr(store, "aggregate_discovered_by_nacos", lambda status="active": fake)


def test_run_service_rolling_happy_path_across_agents(monkeypatch):
    # 两 agent 各若干 matched healthy → 汇成一条列表、全局逐一滚;断言 graceful-restart 次数=总实例数、顺序。
    _patch_agg(monkeypatch, {"svc": ["agent-a", "agent-b"]})
    hub = FakeServiceHubState(
        list_results={
            "agent-a": {"status": "success", "instances": [_inst("ha:1", "a1"), _inst("ha:2", "a2")]},
            "agent-b": {"status": "success", "instances": [_inst("hb:1", "b1")]},
        },
        graceful_results=[{"status": "success"}, {"status": "success"}, {"status": "success"}],
    )
    asyncio.run(rolling_router._run_service_rolling("t1", "svc", False, hub, FakeSettings()))

    assert hub.finished["status"] == "done"
    assert hub.finished["degraded"] is False  # 集群 ≥2 正常完成不标 degraded
    gr = [(aid, m) for aid, m in hub.calls if m["type"] == "graceful-restart"]
    # 总实例数 = 3,全局逐一滚;顺序 = 先 agent-a 两实例(返回序)再 agent-b 一实例。
    assert len(gr) == 3
    assert [aid for aid, _ in gr] == ["agent-a", "agent-a", "agent-b"]
    assert [m["containerId"] for _, m in gr] == ["a1", "a2", "b1"]
    assert gr[2][1]["healthBaseUrl"] == "http://hb:1"
    # 终态所有节点 done,且每个 node 带 agentId。
    assert [n["status"] for n in hub.finished["nodes"]] == ["done", "done", "done"]
    assert [n["agentId"] for n in hub.finished["nodes"]] == ["agent-a", "agent-a", "agent-b"]
    # 推进经过 in-progress。
    assert any(any(n["status"] == "in-progress" for n in snap) for snap in hub.node_updates)


def test_run_service_rolling_list_instances_failed_freezes(monkeypatch):
    # 某 agent list-instances 失败 → freeze(failed,error 注明哪个 agent);不发任何 graceful-restart。
    _patch_agg(monkeypatch, {"svc": ["agent-a", "agent-b"]})
    hub = FakeServiceHubState(
        list_results={
            "agent-a": {"status": "success", "instances": [_inst("ha:1", "a1")]},
            "agent-b": {"status": "failed", "error": "nacos down"},
        },
    )
    asyncio.run(rolling_router._run_service_rolling("t1", "svc", True, hub, FakeSettings()))

    assert hub.finished["status"] == "failed"
    assert "list-instances" in hub.finished["error"]
    assert "agent-b" in hub.finished["error"]
    assert not any(m["type"] == "graceful-restart" for _, m in hub.calls)


def test_run_service_rolling_fail_stop_freezes(monkeypatch):
    # 某实例 graceful-restart 失败 → 失败即停:该节点 failed、余下 skipped、task failed、不继续滚后续。
    _patch_agg(monkeypatch, {"svc": ["agent-a", "agent-b"]})
    hub = FakeServiceHubState(
        list_results={
            "agent-a": {"status": "success", "instances": [_inst("ha:1", "a1")]},
            "agent-b": {"status": "success", "instances": [_inst("hb:1", "b1"), _inst("hb:2", "b2")]},
        },
        graceful_results=[{"status": "success"}, {"status": "failed", "error": "boom"}],
    )
    asyncio.run(rolling_router._run_service_rolling("t1", "svc", False, hub, FakeSettings()))

    assert hub.finished["status"] == "failed"
    gr = [m for _, m in hub.calls if m["type"] == "graceful-restart"]
    assert len(gr) == 2  # 第 1 个成功、第 2 个失败即停,不发第 3 个
    nodes = hub.finished["nodes"]
    assert nodes[0]["status"] == "done"
    assert nodes[1]["status"] == "failed" and nodes[1]["error"] == "boom"
    assert nodes[2]["status"] == "skipped"


def test_run_service_rolling_filters_unmatched(monkeypatch):
    # unmatched 实例被过滤(不再 abort):只滚 matched 且 healthy 的实例。
    _patch_agg(monkeypatch, {"svc": ["agent-a"]})
    hub = FakeServiceHubState(
        list_results={
            "agent-a": {"status": "success", "instances": [
                _inst("ha:1", "a1"),                       # matched + healthy → 滚
                _inst("hb:1", "b1", matched=False),         # 别机实例(unmatched)→ 过滤
                _inst("ha:2", "a2", healthy=False),         # 本机但不健康 → 过滤
                _inst("ha:3", "a3"),                       # matched + healthy → 滚
            ]},
        },
        graceful_results=[{"status": "success"}, {"status": "success"}],
    )
    asyncio.run(rolling_router._run_service_rolling("t1", "svc", False, hub, FakeSettings()))

    assert hub.finished["status"] == "done"
    gr = [m for _, m in hub.calls if m["type"] == "graceful-restart"]
    assert [m["containerId"] for m in gr] == ["a1", "a3"]  # 只滚两个 matched+healthy


def test_run_service_rolling_cluster_total_lt_2_rejected(monkeypatch):
    # 集群 total<2 且非 force → 拒(零中断不可达);不发 graceful-restart。
    _patch_agg(monkeypatch, {"svc": ["agent-a"]})
    hub = FakeServiceHubState(
        list_results={"agent-a": {"status": "success", "instances": [_inst("ha:1", "a1")]}},
    )
    asyncio.run(rolling_router._run_service_rolling("t1", "svc", False, hub, FakeSettings()))

    assert hub.finished["status"] == "failed"
    assert "集群健康实例数=1<2" in hub.finished["error"]
    assert not any(m["type"] == "graceful-restart" for _, m in hub.calls)


def test_run_service_rolling_cluster_total_lt_2_force_degraded(monkeypatch):
    # total<2 但 force 放行 → 滚完标 degraded。
    _patch_agg(monkeypatch, {"svc": ["agent-a"]})
    hub = FakeServiceHubState(
        list_results={"agent-a": {"status": "success", "instances": [_inst("ha:1", "a1")]}},
        graceful_results=[{"status": "success"}],
    )
    asyncio.run(rolling_router._run_service_rolling("t1", "svc", True, hub, FakeSettings()))

    assert hub.finished["status"] == "degraded"
    assert hub.finished["degraded"] is True
    gr = [m for _, m in hub.calls if m["type"] == "graceful-restart"]
    assert len(gr) == 1


def test_run_service_rolling_no_discovered_instances_fails(monkeypatch):
    # 聚合里无该服务(nacos 未发现活跃实例)→ failed,且不调任何 agent。
    _patch_agg(monkeypatch, {"other-svc": ["agent-a"]})
    hub = FakeServiceHubState()
    asyncio.run(rolling_router._run_service_rolling("t1", "svc", True, hub, FakeSettings()))

    assert hub.finished["status"] == "failed"
    assert "无发现实例" in hub.finished["error"]
    assert hub.calls == []


def test_run_service_rolling_all_unmatched_yields_no_targets(monkeypatch):
    # 发现到 agent,但其本机实例全 unmatched(被过滤)→ total==0 → failed(无可滚实例)。
    _patch_agg(monkeypatch, {"svc": ["agent-a"]})
    hub = FakeServiceHubState(
        list_results={"agent-a": {"status": "success", "instances": [
            _inst("hb:1", "b1", matched=False), _inst("hb:2", "b2", matched=False),
        ]}},
    )
    asyncio.run(rolling_router._run_service_rolling("t1", "svc", True, hub, FakeSettings()))

    assert hub.finished["status"] == "failed"
    assert "无可滚实例" in hub.finished["error"]
    assert not any(m["type"] == "graceful-restart" for _, m in hub.calls)


def test_run_service_rolling_timeout_yields_failed(monkeypatch):
    # call_agent 抛 asyncio.TimeoutError → failed 且 error 含"超时"(兜底出口)。
    _patch_agg(monkeypatch, {"svc": ["agent-a"]})
    hub = FakeServiceHubState(call_side_effect=asyncio.TimeoutError())
    asyncio.run(rolling_router._run_service_rolling("t1", "svc", False, hub, FakeSettings()))

    assert hub.finished["status"] == "failed"
    assert "超时" in hub.finished["error"]


def test_run_service_rolling_generic_exception_yields_failed(monkeypatch):
    # call_agent 抛普通 Exception → 通用兜底出口 failed。
    _patch_agg(monkeypatch, {"svc": ["agent-a"]})
    hub = FakeServiceHubState(call_side_effect=RuntimeError("agent 连接不可用"))
    asyncio.run(rolling_router._run_service_rolling("t1", "svc", False, hub, FakeSettings()))

    assert hub.finished["status"] == "failed"
    assert "agent 连接不可用" in hub.finished["error"]


# =====================================================================================
# P5-2:灰度(instance_filter 子集滚)—— 健康门按全集、只滚子集;子集未命中 → failed;None 等价
# =====================================================================================


def test_run_service_rolling_instance_filter_rolls_only_subset(monkeypatch):
    # 全集 2 个 matched+healthy 实例(健康门按全集放行,非 force 也过);instance_filter 只点 1 个 →
    # **只对该 containerId 发 graceful-restart**,另一个不动。degraded 按全集 total=2 → 不标 degraded。
    _patch_agg(monkeypatch, {"svc": ["agent-a", "agent-b"]})
    hub = FakeServiceHubState(
        list_results={
            "agent-a": {"status": "success", "instances": [_inst("ha:1", "a1")]},
            "agent-b": {"status": "success", "instances": [_inst("hb:1", "b1")]},
        },
        graceful_results=[{"status": "success"}],  # 只该滚一个
    )
    asyncio.run(rolling_router._run_service_rolling(
        "t1", "svc", False, hub, FakeSettings(), instance_filter={"b1"}))

    assert hub.finished["status"] == "done"
    assert hub.finished["degraded"] is False  # degraded 按全集判定(全集=2,不降级)
    gr = [(aid, m) for aid, m in hub.calls if m["type"] == "graceful-restart"]
    assert len(gr) == 1  # 只滚子集里的那一个
    assert gr[0][0] == "agent-b"
    assert gr[0][1]["containerId"] == "b1"
    # 落表 nodes 只含子集那一个。
    assert [n["containerId"] for n in hub.finished["nodes"]] == ["b1"]


def test_run_service_rolling_instance_filter_health_gate_uses_full_set(monkeypatch):
    # 健康门按**全集**:全集只 1 个健康实例、非 force → 即便灰度只想滚这 1 个,也被全集门拒(<2)。
    # 证明灰度子集受全集保护(不会绕过集群健康门)。
    _patch_agg(monkeypatch, {"svc": ["agent-a"]})
    hub = FakeServiceHubState(
        list_results={"agent-a": {"status": "success", "instances": [_inst("ha:1", "a1")]}},
    )
    asyncio.run(rolling_router._run_service_rolling(
        "t1", "svc", False, hub, FakeSettings(), instance_filter={"a1"}))

    assert hub.finished["status"] == "failed"
    assert "集群健康实例数=1<2" in hub.finished["error"]  # 全集门拒,非子集逻辑
    assert not any(m["type"] == "graceful-restart" for _, m in hub.calls)


def test_run_service_rolling_instance_filter_no_match_fails(monkeypatch):
    # 全集健康门放行(2 实例),但 instance_filter 指定的 containerId 都不在健康集 → failed(子集未命中)。
    _patch_agg(monkeypatch, {"svc": ["agent-a", "agent-b"]})
    hub = FakeServiceHubState(
        list_results={
            "agent-a": {"status": "success", "instances": [_inst("ha:1", "a1")]},
            "agent-b": {"status": "success", "instances": [_inst("hb:1", "b1")]},
        },
    )
    asyncio.run(rolling_router._run_service_rolling(
        "t1", "svc", False, hub, FakeSettings(), instance_filter={"nope"}))

    assert hub.finished["status"] == "failed"
    assert "灰度子集未命中" in hub.finished["error"]
    assert not any(m["type"] == "graceful-restart" for _, m in hub.calls)


def test_run_service_rolling_instance_filter_none_equivalent_to_full(monkeypatch):
    # 等价回归:instance_filter=None(显式)与不传一致 —— 全量滚。
    _patch_agg(monkeypatch, {"svc": ["agent-a", "agent-b"]})
    hub = FakeServiceHubState(
        list_results={
            "agent-a": {"status": "success", "instances": [_inst("ha:1", "a1")]},
            "agent-b": {"status": "success", "instances": [_inst("hb:1", "b1")]},
        },
        graceful_results=[{"status": "success"}, {"status": "success"}],
    )
    asyncio.run(rolling_router._run_service_rolling(
        "t1", "svc", False, hub, FakeSettings(), instance_filter=None))

    assert hub.finished["status"] == "done"
    gr = [m for _, m in hub.calls if m["type"] == "graceful-restart"]
    assert [m["containerId"] for m in gr] == ["a1", "b1"]  # 全量两个都滚


def test_run_service_rolling_instance_filter_subset_force_degraded(monkeypatch):
    # 灰度 + 全集<2 + force:全集门放行(force),只滚子集那一个,degraded 按全集 total=1 → 标 degraded。
    _patch_agg(monkeypatch, {"svc": ["agent-a"]})
    hub = FakeServiceHubState(
        list_results={"agent-a": {"status": "success", "instances": [_inst("ha:1", "a1")]}},
        graceful_results=[{"status": "success"}],
    )
    asyncio.run(rolling_router._run_service_rolling(
        "t1", "svc", True, hub, FakeSettings(), instance_filter={"a1"}))

    assert hub.finished["status"] == "degraded"
    assert hub.finished["degraded"] is True  # 全集 total=1 → degraded(与是否灰度无关)
