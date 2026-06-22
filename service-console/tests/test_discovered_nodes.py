"""agent 发现上报落地(P3-4/P3-5 后端)测试。

覆盖:
- 建表 + (agent_id, container_name) 唯一约束在 sqlite 上真生效;
- record_discovery 首次插入(字段/status=active/first_seen_at/heartbeat_at);
- 二次同 (agent,container) 更新同一行(行数不变、字段刷新、heartbeat 推进);
- 本轮缺席的行标 stale 且仍在(M8:不删);其它 agent 的行不受影响;
- 跳过 container_name 为空的节点(不建行、不报错);
- discovery-report 经 _handle_agent_message 路由到 record_discovery。

直连库的用例仿 test_models.py:`Database("sqlite:///<tmp>/t.db") + init_schema()`,
store 方法照 test_store_unit.py 用 `asyncio.run(...)`(本仓库测试无 pytest-asyncio)。
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import Database
from app.hub.db_models import DiscoveredNodeModel
from app.hub.store import HubState, utc_now


def _db(tmp_path: Path) -> Database:
    d = Database(f"sqlite:///{tmp_path}/t.db")
    d.init_schema()
    return d


def _state(tmp_path: Path) -> tuple[HubState, Database]:
    d = _db(tmp_path)
    return HubState(heartbeat_timeout=90, command_history_limit=200, database=d), d


def _node(name: str, **overrides) -> dict:
    node = {
        "containerId": f"cid-{name}",
        "containerName": name,
        "composeProject": "proj",
        "composeService": "svc",
        "dir": "/srv/proj",
        "image": "nginx:latest",
        "running": True,
        "nacosService": "memory-share",
        "healthy": True,
    }
    node.update(overrides)
    return node


# --- 建表 + 唯一约束 --------------------------------------------------------


def test_discovered_nodes_table_created(tmp_path: Path) -> None:
    import sqlalchemy as sa

    d = _db(tmp_path)
    assert "discovered_nodes" in sa.inspect(d.engine).get_table_names()


def test_uq_dn_agent_container(tmp_path: Path) -> None:
    d = _db(tmp_path)
    now = datetime.now(timezone.utc)

    def _row(agent_id: str = "agent-a", container_name: str = "c1"):
        return DiscoveredNodeModel(
            agent_id=agent_id,
            container_name=container_name,
            running=False,
            status="active",
            heartbeat_at=now,
            first_seen_at=now,
            created_at=now,
            updated_at=now,
        )

    with d.session_factory() as s:
        s.add(_row())
        s.commit()
    # 同 (agent_id, container_name) 第二行冲突
    with d.session_factory() as s:
        s.add(_row())
        with pytest.raises(IntegrityError):
            s.commit()
    # 不同 agent 下同 container_name 允许
    with d.session_factory() as s:
        s.add(_row(agent_id="agent-b"))
        s.commit()


# --- record_discovery 首次插入 ---------------------------------------------


def test_record_discovery_inserts_row(tmp_path: Path) -> None:
    state, d = _state(tmp_path)

    asyncio.run(state.record_discovery("agent-a", [_node("c1")], []))

    with d.session_factory() as s:
        rows = s.scalars(select(DiscoveredNodeModel)).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.agent_id == "agent-a"
    assert r.container_name == "c1"
    assert r.container_id == "cid-c1"
    assert r.compose_project == "proj"
    assert r.compose_service == "svc"
    assert r.dir == "/srv/proj"
    assert r.image == "nginx:latest"
    assert r.running is True
    assert r.nacos_service == "memory-share"
    assert r.healthy is True
    assert r.status == "active"
    assert r.first_seen_at is not None
    assert r.heartbeat_at is not None

    d.engine.dispose()


def test_record_discovery_running_false_and_healthy_none(tmp_path: Path) -> None:
    # 已停容器:running=False;未匹配 nacos:healthy=None(歧义/未注册)。
    state, d = _state(tmp_path)

    asyncio.run(
        state.record_discovery(
            "agent-a",
            [_node("c1", running=False, healthy=None, nacosService=None)],
            [],
        )
    )

    with d.session_factory() as s:
        r = s.scalar(select(DiscoveredNodeModel).where(DiscoveredNodeModel.container_name == "c1"))
    assert r is not None
    assert r.running is False
    assert r.healthy is None
    assert r.nacos_service is None

    d.engine.dispose()


# --- record_discovery 二次同键更新 -----------------------------------------


def test_record_discovery_second_round_updates_same_row(tmp_path: Path) -> None:
    state, d = _state(tmp_path)

    asyncio.run(state.record_discovery("agent-a", [_node("c1", image="nginx:1.0", running=True)], []))
    with d.session_factory() as s:
        first = s.scalar(select(DiscoveredNodeModel).where(DiscoveredNodeModel.container_name == "c1"))
        first_seen = first.first_seen_at
        first_hb = first.heartbeat_at
        row_id = first.id

    # 二次上报:同 (agent,container),字段变化(镜像升级、变为已停)。
    asyncio.run(state.record_discovery("agent-a", [_node("c1", image="nginx:2.0", running=False)], []))

    with d.session_factory() as s:
        rows = s.scalars(select(DiscoveredNodeModel)).all()
        assert len(rows) == 1  # 仍是同一行,未新增
        r = rows[0]
        assert r.id == row_id
        assert r.image == "nginx:2.0"
        assert r.running is False
        assert r.status == "active"
        assert r.first_seen_at == first_seen  # 首见时间不变
        assert r.heartbeat_at >= first_hb  # 心跳推进(>= 容忍同一时钟刻度)

    d.engine.dispose()


# --- 缺席标 stale 不删 + 不影响其它 agent -----------------------------------


def test_absent_container_marked_stale_not_deleted(tmp_path: Path) -> None:
    state, d = _state(tmp_path)

    # 第一轮:c1、c2 两个容器。
    asyncio.run(state.record_discovery("agent-a", [_node("c1"), _node("c2")], []))
    # 第二轮:只剩 c1(c2 缺席)。
    asyncio.run(state.record_discovery("agent-a", [_node("c1")], []))

    with d.session_factory() as s:
        c1 = s.scalar(select(DiscoveredNodeModel).where(DiscoveredNodeModel.container_name == "c1"))
        c2 = s.scalar(select(DiscoveredNodeModel).where(DiscoveredNodeModel.container_name == "c2"))
    assert c1 is not None and c1.status == "active"
    assert c2 is not None  # 仍在,未删
    assert c2.status == "stale"

    d.engine.dispose()


def test_stale_does_not_affect_other_agents(tmp_path: Path) -> None:
    state, d = _state(tmp_path)

    asyncio.run(state.record_discovery("agent-a", [_node("c1")], []))
    asyncio.run(state.record_discovery("agent-b", [_node("c1")], []))
    # agent-a 本轮没有任何节点 → 其 c1 应标 stale;agent-b 不受影响。
    asyncio.run(state.record_discovery("agent-a", [], []))

    with d.session_factory() as s:
        a = s.scalar(
            select(DiscoveredNodeModel).where(
                DiscoveredNodeModel.agent_id == "agent-a",
                DiscoveredNodeModel.container_name == "c1",
            )
        )
        b = s.scalar(
            select(DiscoveredNodeModel).where(
                DiscoveredNodeModel.agent_id == "agent-b",
                DiscoveredNodeModel.container_name == "c1",
            )
        )
    assert a is not None and a.status == "stale"
    assert b is not None and b.status == "active"

    d.engine.dispose()


def test_stale_row_revives_to_active_on_return(tmp_path: Path) -> None:
    # 缺席→stale 的节点再次出现应复活为 active(同一行)。
    state, d = _state(tmp_path)

    asyncio.run(state.record_discovery("agent-a", [_node("c1")], []))
    asyncio.run(state.record_discovery("agent-a", [], []))  # c1 → stale
    asyncio.run(state.record_discovery("agent-a", [_node("c1")], []))  # 复现

    with d.session_factory() as s:
        rows = s.scalars(select(DiscoveredNodeModel)).all()
    assert len(rows) == 1
    assert rows[0].status == "active"

    d.engine.dispose()


# --- 跳过空 container_name --------------------------------------------------


def test_skip_node_without_container_name(tmp_path: Path) -> None:
    state, d = _state(tmp_path)

    # 三个节点:一个名为空串、一个无该键、一个正常 → 只应落 1 行。
    nodes = [
        _node("", containerName=""),
        {"containerId": "x", "running": True},  # 无 containerName
        _node("c1"),
    ]
    asyncio.run(state.record_discovery("agent-a", nodes, []))

    with d.session_factory() as s:
        rows = s.scalars(select(DiscoveredNodeModel)).all()
    assert len(rows) == 1
    assert rows[0].container_name == "c1"

    d.engine.dispose()


def test_record_discovery_all_invalid_nodes_no_rows(tmp_path: Path) -> None:
    # 整轮都是无效节点:不建行、不报错(seen_names 为空分支)。
    state, d = _state(tmp_path)

    asyncio.run(state.record_discovery("agent-a", [{"containerId": "x"}], []))

    with d.session_factory() as s:
        rows = s.scalars(select(DiscoveredNodeModel)).all()
    assert rows == []

    d.engine.dispose()


# --- warnings 只记日志不入库 ------------------------------------------------


def test_warnings_logged_not_persisted(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    state, d = _state(tmp_path)
    warnings = [{"type": "instance-multi-container", "nacosService": "memory-share"}]

    with caplog.at_level(logging.WARNING, logger="app.hub.store"):
        asyncio.run(state.record_discovery("agent-a", [_node("c1")], warnings))

    # 警告进了日志。
    assert any("落位冲突" in rec.getMessage() for rec in caplog.records)
    # 但只落了节点行,没有为 warning 建任何额外行。
    with d.session_factory() as s:
        rows = s.scalars(select(DiscoveredNodeModel)).all()
    assert len(rows) == 1

    d.engine.dispose()


# --- 路由:discovery-report 经 _handle_agent_message 到 record_discovery -----


def test_handle_agent_message_routes_discovery_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module
    import app.hub.api_support as api_support

    state, d = _state(tmp_path)
    monkeypatch.setattr(main_module, "hub_state", state)

    calls: list[tuple] = []

    async def fake_record(agent_id, nodes, warnings):
        calls.append((agent_id, nodes, warnings))

    monkeypatch.setattr(state, "record_discovery", fake_record)

    asyncio.run(
        api_support._handle_agent_message(
            "agent-a",
            {
                "type": "discovery-report",
                "agentId": "agent-a",
                "nodes": [_node("c1")],
                "warnings": [{"type": "container-multi-instance"}],
                "ts": 123,
            },
        )
    )

    assert len(calls) == 1
    agent_id, nodes, warnings = calls[0]
    assert agent_id == "agent-a"
    assert nodes[0]["containerName"] == "c1"
    assert warnings == [{"type": "container-multi-instance"}]

    d.engine.dispose()


def test_handle_agent_message_discovery_report_missing_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # nodes/warnings 缺省时应以空列表兜底(payload.get(...) or []),并真正落库(端到端)。
    import app.main as main_module
    import app.hub.api_support as api_support

    state, d = _state(tmp_path)
    monkeypatch.setattr(main_module, "hub_state", state)

    asyncio.run(api_support._handle_agent_message("agent-a", {"type": "discovery-report"}))

    # 无节点:不建行也不报错。
    with d.session_factory() as s:
        rows = s.scalars(select(DiscoveredNodeModel)).all()
    assert rows == []

    d.engine.dispose()
