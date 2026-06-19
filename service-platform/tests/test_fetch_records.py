"""获取记录列表测试(Task 11.5,评审 H1):服务端分页 + LEFT JOIN 名称。

`GET /api/fetch-records`(`require_session` 保护):信封 {count,rows,page,pageSize,
totalPage};可选 ?namespaceId=/?serviceId= 过滤;rows 经 LEFT JOIN 带只读
namespaceCode/serviceCode/pluginCode/version(评审 H3),出 camelCase。

P1-SPA「获取记录」页依赖此端点;原 P1a 只「写」fetch_record(Task 11)无「读列表」,
会让该页 404 —— 本任务补齐。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import store
from app.db_models import Namespace, Plugin, PluginVersion, Service


def _h(client: TestClient) -> dict[str, str]:
    """登录拿 JWT,组装 Authorization 头(require_session 保护)。"""
    token = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _seed_namespace(ns_code: str, *, plugin_code: str, version: str) -> dict:
    """造 namespace/service/plugin/plugin_version,便于 fetch_record 的 LEFT JOIN 回名。"""
    ns = store.create_row(Namespace, {"code": ns_code})
    svc = store.create_row(Service, {"namespace_id": ns.id, "service_code": f"{ns_code}-svc"})
    plugin = store.create_row(Plugin, {"code": plugin_code})
    pv = store.create_row(PluginVersion, {"plugin_id": plugin.id, "version": version})
    return {
        "namespaceId": ns.id,
        "serviceId": svc.id,
        "pluginId": plugin.id,
        "pluginVersionId": pv.id,
        "nsCode": ns_code,
        "serviceCode": svc.service_code,
        "pluginCode": plugin_code,
        "version": version,
    }


def _write_records(seed: dict, n: int) -> None:
    for _ in range(n):
        store.create_fetch_record(
            namespace_id=seed["namespaceId"],
            service_id=seed["serviceId"],
            plugin_id=seed["pluginId"],
            plugin_version_id=seed["pluginVersionId"],
        )


def test_fetch_records_envelope_and_join_names(client: TestClient) -> None:
    """信封形状正确 + 分页生效(len(rows)<=pageSize)+ rows 含 JOIN 回的只读名称。"""
    h = _h(client)
    a = _seed_namespace("ns-a", plugin_code="@business/plugin-x", version="1.2.3")
    _write_records(a, 3)

    body = client.get("/api/fetch-records?page=1&pageSize=2", headers=h).json()
    # 信封字段(camelCase)
    assert set(body.keys()) >= {"count", "rows", "page", "pageSize", "totalPage"}
    assert body["count"] == 3
    assert body["page"] == 1 and body["pageSize"] == 2
    assert body["totalPage"] == 2  # ceil(3/2)
    assert len(body["rows"]) <= 2  # 服务端分页

    row = body["rows"][0]
    # LEFT JOIN 回只读名称(评审 H3)
    assert row["namespaceCode"] == "ns-a"
    assert row["serviceCode"] == "ns-a-svc"
    assert row["pluginCode"] == "@business/plugin-x"
    assert row["version"] == "1.2.3"
    # 本行字段 camelCase
    assert row["namespaceId"] == a["namespaceId"]
    assert "fetchDate" in row


def test_fetch_records_filter_by_namespace(client: TestClient) -> None:
    """?namespaceId= 过滤生效:只回该 namespace 的记录。"""
    h = _h(client)
    a = _seed_namespace("ns-a", plugin_code="@business/plugin-x", version="1.0.0")
    b = _seed_namespace("ns-b", plugin_code="@business/plugin-y", version="2.0.0")
    _write_records(a, 2)
    _write_records(b, 3)

    body = client.get(f"/api/fetch-records?namespaceId={a['namespaceId']}", headers=h).json()
    assert body["count"] == 2
    assert all(r["namespaceId"] == a["namespaceId"] for r in body["rows"])

    body_b = client.get(f"/api/fetch-records?namespaceId={b['namespaceId']}", headers=h).json()
    assert body_b["count"] == 3


def test_fetch_records_filter_by_service(client: TestClient) -> None:
    """?serviceId= 过滤生效。"""
    h = _h(client)
    a = _seed_namespace("ns-a", plugin_code="@business/plugin-x", version="1.0.0")
    b = _seed_namespace("ns-b", plugin_code="@business/plugin-y", version="2.0.0")
    _write_records(a, 1)
    _write_records(b, 4)

    body = client.get(f"/api/fetch-records?serviceId={b['serviceId']}", headers=h).json()
    assert body["count"] == 4
    assert all(r["serviceId"] == b["serviceId"] for r in body["rows"])


def test_fetch_records_requires_auth(client: TestClient) -> None:
    """无 Bearer → default-deny 中间件 401(/api/fetch-records 非白名单)。"""
    assert client.get("/api/fetch-records").status_code == 401
