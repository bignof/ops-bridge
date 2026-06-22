"""hub_client 单测(S5:进程内适配器)。

历史:hub_client 原是「平台用 admin token 经 httpx 调外部 service-hub」的 HTTP 客户端,本文件原先
monkeypatch `hc.httpx.post/get` 断言 URL/header/body。S5 把 hub 并入同进程后,hub_client 重写为
**进程内适配器**——直调 `app/hub/` 的路由 handler / `_build_command_list_response`,不再走 httpx。
故本文件整体重写为**进程内端到端**验证:经 conftest `client` fixture(它把 `hub_state` 指向隔离临时
库、设 `admin_token` 测试值、跑 lifespan 初始化),真跑适配器 → hub_state,断言返回形状与失败归一化。

异步驱动:本仓库未装 pytest-asyncio,故测试函数保持 **sync**,用 `asyncio.run(...)` 驱动被测的
async 适配器函数(`TestClient` 的 anyio portal 跑在后台线程,不占用主线程事件循环,主线程 asyncio.run
安全)。`client` fixture 仅用于环境装配(隔离库 + hub_state 接线 + admin_token),无需真发 HTTP。

覆盖要点:
- provision/rotate:真实签发 agentKey(非空 str);provision 同 id 二次 → hub 409 → 归一化 HubError;
  缺 key / handler 失败统一 HubError(契约供 namespaces 502 映射复用)。
- list_agents:返回 camelCase dict 列表(agentId/online/lastSeenAt),供节点页消费。
- list_instances / dispatch:离线 agent → handler 抛 HTTPException(由 nodes fan-out / dispatch 兜底)。
- rolling_restart:返回 {taskId}(后台任务异步跑,本函数同步返回句柄)。
- list_commands:page/pageSize → limit/offset 换算正确;返回 camelCase 信封。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import app.hub_client as hc


def _run(coro):
    """主线程驱动 async 适配器(无 pytest-asyncio;TestClient portal 在后台线程,主线程无运行中 loop)。"""
    return asyncio.run(coro)


# ── provision_agent ───────────────────────────────────────────────────────────────


def test_provision_agent_issues_key(client) -> None:
    # 进程内 provision 真落 hub agents 表,返回非空 agentKey(show-once 密钥)。
    key = _run(hc.provision_agent("ns-prov-1"))
    assert isinstance(key, str) and key  # 真实签发的密钥非空


def test_provision_agent_duplicate_raises_hub_error(client) -> None:
    # 同 agentId 二次 provision → hub handler 抛 409(already exists)→ 适配器归一化为 HubError。
    _run(hc.provision_agent("ns-dup"))
    with pytest.raises(hc.HubError):
        _run(hc.provision_agent("ns-dup"))


def test_provision_agent_wraps_handler_failure_as_hub_error(client, monkeypatch) -> None:
    # handler 任意失败(此处模拟返回缺 agent_key 的响应)→ 适配器归一化 HubError(供 namespaces 502 映射)。
    class _NoKey:
        agent_key = ""  # 缺 key

    async def fake_handler(*a, **k):
        return _NoKey()

    import app.hub.routers.agents as agents_mod

    monkeypatch.setattr(agents_mod, "provision_agent", fake_handler)
    with pytest.raises(hc.HubError):
        _run(hc.provision_agent("ns-nokey"))


# ── rotate_agent_key ──────────────────────────────────────────────────────────────


def test_rotate_agent_key_returns_new_key(client) -> None:
    # 先 provision 再 rotate:返回新 agentKey(非空 str)。
    first = _run(hc.provision_agent("ns-rot"))
    rotated = _run(hc.rotate_agent_key("ns-rot"))
    assert isinstance(rotated, str) and rotated
    assert rotated != first  # 轮换后是新密钥


def test_rotate_agent_key_wraps_failure_as_hub_error(client, monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("hub 内部炸了")

    import app.hub.routers.agents as agents_mod

    monkeypatch.setattr(agents_mod, "rotate_agent_credentials", boom)
    with pytest.raises(hc.HubError):
        _run(hc.rotate_agent_key("ns-rot-fail"))


# ── list_agents ───────────────────────────────────────────────────────────────────


def test_list_agents_returns_camel_dicts(client) -> None:
    # provision 两个 agent 后,list_agents 返回 camelCase dict 列表(节点页据 agentId/online/lastSeenAt 消费)。
    _run(hc.provision_agent("ns-a1"))
    _run(hc.provision_agent("ns-a2"))
    agents = _run(hc.list_agents())
    ids = [a["agentId"] for a in agents]
    assert "ns-a1" in ids and "ns-a2" in ids
    # 形状:每项含节点页读取的 camelCase 字段;离线(未连 WS)→ online False。
    a1 = next(a for a in agents if a["agentId"] == "ns-a1")
    assert a1["online"] is False  # 仅 provision、未连 WS → 离线
    assert "lastSeenAt" in a1  # camelCase 序列化(snake 不应出现)
    assert "agent_id" not in a1


def test_list_agents_empty(client) -> None:
    # 无 agent → 空列表(不报错)。
    assert _run(hc.list_agents()) == []


# ── list_instances ────────────────────────────────────────────────────────────────


def test_list_instances_offline_agent_raises_http_exception(client) -> None:
    # 仅 provision(未连 WS)的 agent 离线 → handler 抛 HTTPException(409 offline);
    # 节点页 fan-out 的 gather(return_exceptions=True) 据此把该行标 degraded(本任务核心不变式的底层)。
    _run(hc.provision_agent("ns-li-off"))
    with pytest.raises(HTTPException) as ei:
        _run(hc.list_instances("ns-li-off", "some-svc"))
    assert ei.value.status_code == 409  # Agent is offline


def test_list_instances_unknown_agent_raises_http_exception(client) -> None:
    # 完全不存在的 agent → handler 抛 HTTPException(404)。
    with pytest.raises(HTTPException) as ei:
        _run(hc.list_instances("no-such-agent", "svc"))
    assert ei.value.status_code == 404


def test_list_instances_passes_expected_compose_project(client, monkeypatch) -> None:
    # expected_compose_project 透传:断言进到 handler 的 ListInstancesRequest.expectedComposeProject 正确。
    captured = {}

    async def fake_handler(request, agent_id, admin_token):
        captured["expected"] = request.expectedComposeProject
        captured["agent_id"] = agent_id
        captured["service"] = request.serviceName
        from app.hub.models import ListInstancesResponse

        return ListInstancesResponse(status="success", instances=[])

    import app.hub.routers.commands as commands_mod

    monkeypatch.setattr(commands_mod, "list_agent_instances", fake_handler)
    out = _run(hc.list_instances("a1", "svc-x", expected_compose_project="proj-1"))
    assert out == {"status": "success", "instances": []}  # model_dump(by_alias) 形状
    assert captured["expected"] == "proj-1"
    assert captured["agent_id"] == "a1"
    assert captured["service"] == "svc-x"


def test_list_instances_empty_expected_compose_project_becomes_none(client, monkeypatch) -> None:
    # 空串 expected → 进 handler 时归一化为 None(不误触 agent 工程漂移守卫)。
    captured = {}

    async def fake_handler(request, agent_id, admin_token):
        captured["expected"] = request.expectedComposeProject
        from app.hub.models import ListInstancesResponse

        return ListInstancesResponse(status="success", instances=[])

    import app.hub.routers.commands as commands_mod

    monkeypatch.setattr(commands_mod, "list_agent_instances", fake_handler)
    _run(hc.list_instances("a1", "svc", expected_compose_project=""))
    assert captured["expected"] is None


# ── dispatch_command ──────────────────────────────────────────────────────────────


def test_dispatch_command_offline_agent_raises_http_exception(client) -> None:
    # provision(离线)agent dispatch → handler 在 online 检查处抛 409;BFF dispatch 的 except 兜底脱敏。
    _run(hc.provision_agent("ns-disp-off"))
    with pytest.raises(HTTPException) as ei:
        _run(hc.dispatch_command("ns-disp-off", {"action": "start", "dir": "/opt/x"}))
    assert ei.value.status_code == 409  # Agent is offline


def test_dispatch_command_derives_requested_by_platform_admin(client, monkeypatch) -> None:
    # 安全(要求3):requested_by 进程内服务端派生为 platform-admin,且 BFF 不自报(requested_by_hint=None)。
    captured = {}

    async def fake_handler(request, agent_id, admin_token, requested_by_hint, request_source):
        captured["requested_by_hint"] = requested_by_hint
        captured["admin_token"] = admin_token
        from app.hub.models import CommandDispatchResponse, CommandSnapshot

        # 用派生身份回填一条命令快照,验证适配器把 hub 的派生身份原样透出。
        from app.hub.api_support import _derive_requested_by

        snap = CommandSnapshot(
            request_id="r1", agent_id=agent_id, status="queued", action=request.action, dir=request.dir,
            requested_by=_derive_requested_by(admin_token), payload={}, created_at="2026-06-21T00:00:00Z",
            updated_at="2026-06-21T00:00:00Z",
        )
        return CommandDispatchResponse(accepted=True, command=snap)

    import app.hub.routers.commands as commands_mod

    monkeypatch.setattr(commands_mod, "dispatch_command", fake_handler)
    out = _run(hc.dispatch_command("a1", {"action": "start", "dir": "/opt/x"}))
    # BFF 绝不自报 requested_by:hint 必为 None(不可伪造,由 hub 据 token 派生)。
    assert captured["requested_by_hint"] is None
    # 适配器把进程内 admin_token 传给 handler(满足 _require_admin_token);hub 据它派生 platform-admin。
    assert captured["admin_token"] == "test-admin-token"
    assert out["command"]["requestedBy"] == "platform-admin"
    assert out["accepted"] is True
    assert out["command"]["requestId"] == "r1"  # camelCase 透出


# ── rolling_restart ───────────────────────────────────────────────────────────────


def test_rolling_restart_returns_task_id(client) -> None:
    # rolling_restart 同步返回 {taskId}(后台 _run_rolling 异步跑,会因 agent 离线而失败,但句柄已返回)。
    _run(hc.provision_agent("ns-roll"))
    out = _run(hc.rolling_restart("ns-roll", "roll-svc", force=False))
    assert "taskId" in out and out["taskId"]


def test_rolling_restart_conflict_raises_http_exception(client, monkeypatch) -> None:
    # 同 (agent,service) 已有滚动在进行 → RollingConflict → handler 409。用桩制造冲突,验证异常透出。
    import app.main as main_module
    from app.hub.store import RollingConflict

    async def boom(*a, **k):
        raise RollingConflict("ns-rc:rc-svc 已有滚动在进行")

    monkeypatch.setattr(main_module.hub_state, "create_rolling_task", boom)
    with pytest.raises(HTTPException) as ei:
        _run(hc.rolling_restart("ns-rc", "rc-svc"))
    assert ei.value.status_code == 409


# ── list_commands ─────────────────────────────────────────────────────────────────


def test_list_commands_empty_envelope(client) -> None:
    # 无命令 → camelCase 信封,total=0,items=[]。
    out = _run(hc.list_commands(page=1, page_size=20))
    assert out["total"] == 0
    assert out["items"] == []
    # camelCase 信封字段齐(hasMore/sortBy/order),snake 不应出现。
    assert "hasMore" in out and "sortBy" in out and "order" in out
    assert "has_more" not in out and "sort_by" not in out


def test_list_commands_paging_to_limit_offset(client, monkeypatch) -> None:
    # page/pageSize → hub limit/offset(limit=pageSize, offset=(page-1)*pageSize)。打桩底层实现断言换算。
    captured = {}

    async def fake_build(**kwargs):
        captured.update(kwargs)
        from app.hub.models import CommandListResponse

        return CommandListResponse(
            items=[], total=0, limit=kwargs["limit"], offset=kwargs["offset"], has_more=False,
            sort_by="createdAt", order="desc",
        )

    import app.hub.api_support as api_support_mod

    monkeypatch.setattr(api_support_mod, "_build_command_list_response", fake_build)
    _run(hc.list_commands(page=3, page_size=10))
    assert captured["limit"] == 10
    assert captured["offset"] == 20  # (3-1)*10


def test_list_commands_clamps_page_floor(client, monkeypatch) -> None:
    # page/pageSize < 1 被夹到 1(offset 不为负)。
    captured = {}

    async def fake_build(**kwargs):
        captured.update(kwargs)
        from app.hub.models import CommandListResponse

        return CommandListResponse(
            items=[], total=0, limit=kwargs["limit"], offset=kwargs["offset"], has_more=False,
            sort_by="createdAt", order="desc",
        )

    import app.hub.api_support as api_support_mod

    monkeypatch.setattr(api_support_mod, "_build_command_list_response", fake_build)
    _run(hc.list_commands(page=0, page_size=0))
    assert captured["limit"] == 1
    assert captured["offset"] == 0
