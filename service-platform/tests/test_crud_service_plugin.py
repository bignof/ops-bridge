"""service_plugin 关联资源端到端测试(Task 6b)。

经 conftest 的 `client` fixture。验证点:
- list/create/destroy 全链路(**无 update**)。
- **级联过滤(评审 M3)**:`?serviceId=` 真过滤(只返回选中 service 下的关联)。
- **LEFT JOIN 名称列(评审 H3)**:列表行含 `namespaceCode`(经 service→namespace 两跳)/
  `serviceCode` / `pluginCode`,值 = 各关联表的 code。
- 唯一约束冲突 `UNIQUE(service_id, plugin_id)` → 409。
- 无 Bearer → default-deny 中间件 401。

父行(namespace/service/plugin)直接经 store 落库(绕过 namespace create 的 hub 调用),
保持本文件聚焦关联资源。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import store
from app.db_models import Namespace, Plugin, Service


def _h(client: TestClient) -> dict[str, str]:
    token = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _mk_ns(code: str) -> int:
    return store.create_row(Namespace, {"code": code, "name": None}).id


def _mk_service(ns_id: int, code: str) -> int:
    return store.create_row(Service, {"namespace_id": ns_id, "service_code": code}).id


def _mk_plugin(code: str) -> int:
    return store.create_row(Plugin, {"code": code, "name": None}).id


def test_service_plugin_create_list_destroy(client: TestClient) -> None:
    h = _h(client)
    ns_id = _mk_ns("sp-ns")
    svc_id = _mk_service(ns_id, "sp-svc")
    plg_id = _mk_plugin("sp-plg")

    # create → 201
    r = client.post("/api/service-plugins", json={"serviceId": svc_id, "pluginId": plg_id}, headers=h)
    assert r.status_code == 201
    link_id = r.json()["id"]
    assert r.json()["serviceId"] == svc_id
    assert r.json()["pluginId"] == plg_id

    # list 信封
    body = client.get("/api/service-plugins", headers=h).json()
    assert body["count"] >= 1 and "totalPage" in body
    assert body["page"] == 1 and body["pageSize"] == 20
    assert any(row["id"] == link_id for row in body["rows"])

    # destroy → 204
    assert client.delete(f"/api/service-plugins/{link_id}", headers=h).status_code == 204
    # 删后列表不再含该行
    body2 = client.get("/api/service-plugins", headers=h).json()
    assert all(row["id"] != link_id for row in body2["rows"])


def test_service_plugin_no_update_route(client: TestClient) -> None:
    # 评审:仅 list/create/destroy,无 update —— PATCH 应 405(方法不允许)。
    h = _h(client)
    ns_id = _mk_ns("noup-ns")
    svc_id = _mk_service(ns_id, "noup-svc")
    plg_id = _mk_plugin("noup-plg")
    link_id = client.post("/api/service-plugins", json={"serviceId": svc_id, "pluginId": plg_id}, headers=h).json()["id"]
    assert client.patch(f"/api/service-plugins/{link_id}", json={"pluginId": plg_id}, headers=h).status_code == 405


def test_service_plugin_left_join_names(client: TestClient) -> None:
    # 评审 H3:LEFT JOIN 回 namespaceCode/serviceCode/pluginCode。
    h = _h(client)
    ns_id = _mk_ns("jn-ns")
    svc_id = _mk_service(ns_id, "jn-svc")
    plg_id = _mk_plugin("jn-plg")
    client.post("/api/service-plugins", json={"serviceId": svc_id, "pluginId": plg_id}, headers=h)

    body = client.get("/api/service-plugins", headers=h).json()
    row = next(r for r in body["rows"] if r["serviceId"] == svc_id and r["pluginId"] == plg_id)
    assert row["namespaceCode"] == "jn-ns"
    assert row["serviceCode"] == "jn-svc"
    assert row["pluginCode"] == "jn-plg"


def test_service_plugin_cascade_filter_by_service(client: TestClient) -> None:
    # 评审 M3:?serviceId= 真过滤。
    h = _h(client)
    ns_id = _mk_ns("flt-sp-ns")
    svc_a = _mk_service(ns_id, "flt-svc-a")
    svc_b = _mk_service(ns_id, "flt-svc-b")
    plg = _mk_plugin("flt-sp-plg")
    client.post("/api/service-plugins", json={"serviceId": svc_a, "pluginId": plg}, headers=h)
    client.post("/api/service-plugins", json={"serviceId": svc_b, "pluginId": plg}, headers=h)

    filtered = client.get(f"/api/service-plugins?serviceId={svc_a}", headers=h).json()
    svc_ids = {r["serviceId"] for r in filtered["rows"]}
    assert svc_a in svc_ids
    assert svc_b not in svc_ids  # 真过滤:B service 的关联不出现


def test_service_plugin_unique_conflict(client: TestClient) -> None:
    # UNIQUE(service_id, plugin_id):同对 → 409。
    h = _h(client)
    ns_id = _mk_ns("uq-sp-ns")
    svc_id = _mk_service(ns_id, "uq-sp-svc")
    plg_id = _mk_plugin("uq-sp-plg")
    assert client.post("/api/service-plugins", json={"serviceId": svc_id, "pluginId": plg_id}, headers=h).status_code == 201
    assert client.post("/api/service-plugins", json={"serviceId": svc_id, "pluginId": plg_id}, headers=h).status_code == 409


def test_service_plugin_destroy_missing_404(client: TestClient) -> None:
    h = _h(client)
    assert client.delete("/api/service-plugins/999999", headers=h).status_code == 404


def test_service_plugin_requires_auth(client: TestClient) -> None:
    assert client.get("/api/service-plugins").status_code == 401
    assert client.post("/api/service-plugins", json={"serviceId": 1, "pluginId": 1}).status_code == 401
