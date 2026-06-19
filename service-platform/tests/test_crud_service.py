"""service 资源端到端 CRUD 测试(Task 6b)。

经 conftest 的 `client` fixture(临时文件库 + swap 单例)。验证点:
- 增删改查全链路。
- **H2 HTTP 层(6a reviewer 要求)**:用 camelCase 请求体 POST 成功 + **响应 JSON 无 snake key**
  (不含 `service_code`/`nacos_service_name`/`default_image`/`namespace_id`),把 camelCase
  往返钉死在 HTTP 层。
- **级联过滤(评审 M3)**:`?namespaceId=` 真过滤(选 A 命名空间只返回 A 下的 service)。
- **LEFT JOIN 名称列(评审 H3)**:列表行含 `namespaceCode`,值 = 关联 namespace.code。
- 唯一约束冲突 `UNIQUE(namespace_id, service_code)` → 409。
- 无 Bearer → default-deny 中间件 401。

注:service 无 hub 依赖,故本文件不打桩 provision_agent;仅创建 namespace 父行时需要,
故下方 `_mk_ns` 直接走 store 落库(绕过 namespace create 的 hub 调用),保持 service 测试纯净。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import store
from app.db_models import Namespace


def _h(client: TestClient) -> dict[str, str]:
    token = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _mk_ns(code: str, name: str | None = None) -> int:
    """直接经 store 落一条 namespace(绕过 hub provision),返回 id。"""
    row = store.create_row(Namespace, {"code": code, "name": name})
    return row.id


def test_service_create_camel_roundtrip_no_snake_keys(client: TestClient) -> None:
    # 评审 H2:camelCase 请求体 POST 成功 + 响应无 snake key。
    h = _h(client)
    ns_id = _mk_ns("ns-a", "命名A")
    r = client.post(
        "/api/services",
        json={
            "namespaceId": ns_id,
            "serviceCode": "svc-a",
            "name": "服务A",
            "dir": "/opt/svc-a",
            "defaultImage": "registry/svc-a:1.0",
            "nacosServiceName": "svc-a-nacos",
        },
        headers=h,
    )
    assert r.status_code == 201
    data = r.json()
    # camelCase 值回得来
    assert data["serviceCode"] == "svc-a"
    assert data["defaultImage"] == "registry/svc-a:1.0"
    assert data["nacosServiceName"] == "svc-a-nacos"
    assert data["namespaceId"] == ns_id
    # 响应里**不得**出现任何 snake key(把 camel 契约钉死在 HTTP 层)
    for snake in ("service_code", "nacos_service_name", "default_image", "namespace_id"):
        assert snake not in data


def test_service_list_left_join_namespace_code(client: TestClient) -> None:
    # 评审 H3:列表 LEFT JOIN 回 namespaceCode。
    h = _h(client)
    ns_id = _mk_ns("ns-join", "JoinNS")
    client.post("/api/services", json={"namespaceId": ns_id, "serviceCode": "svc-join"}, headers=h)
    body = client.get("/api/services", headers=h).json()
    row = next(r for r in body["rows"] if r["serviceCode"] == "svc-join")
    assert row["namespaceCode"] == "ns-join"


def test_service_cascade_filter_by_namespace(client: TestClient) -> None:
    # 评审 M3:?namespaceId= 真过滤,只返回选中命名空间下的 service。
    h = _h(client)
    ns_a = _mk_ns("flt-a")
    ns_b = _mk_ns("flt-b")
    client.post("/api/services", json={"namespaceId": ns_a, "serviceCode": "in-a"}, headers=h)
    client.post("/api/services", json={"namespaceId": ns_b, "serviceCode": "in-b"}, headers=h)

    filtered = client.get(f"/api/services?namespaceId={ns_a}", headers=h).json()
    codes = {r["serviceCode"] for r in filtered["rows"]}
    assert "in-a" in codes
    assert "in-b" not in codes  # 真过滤:B 命名空间的 service 不出现
    assert all(r["namespaceId"] == ns_a for r in filtered["rows"])


def test_service_crud(client: TestClient) -> None:
    h = _h(client)
    ns_id = _mk_ns("ns-crud-svc")
    r = client.post("/api/services", json={"namespaceId": ns_id, "serviceCode": "svc-crud", "name": "S"}, headers=h)
    assert r.status_code == 201
    sid = r.json()["id"]

    # list 信封
    body = client.get("/api/services", headers=h).json()
    assert body["count"] >= 1 and "totalPage" in body
    assert body["page"] == 1 and body["pageSize"] == 20

    # get
    got = client.get(f"/api/services/{sid}", headers=h)
    assert got.status_code == 200 and got.json()["serviceCode"] == "svc-crud"

    # patch 局部更新
    patched = client.patch(f"/api/services/{sid}", json={"name": "S2"}, headers=h)
    assert patched.status_code == 200
    assert patched.json()["name"] == "S2"
    assert patched.json()["serviceCode"] == "svc-crud"  # 未传字段保持

    # delete
    assert client.delete(f"/api/services/{sid}", headers=h).status_code == 204
    assert client.get(f"/api/services/{sid}", headers=h).status_code == 404


def test_service_unique_conflict(client: TestClient) -> None:
    # UNIQUE(namespace_id, service_code):同命名空间下同 code → 409;不同命名空间同 code 不冲突。
    h = _h(client)
    ns_a = _mk_ns("uq-a")
    ns_b = _mk_ns("uq-b")
    assert client.post("/api/services", json={"namespaceId": ns_a, "serviceCode": "same"}, headers=h).status_code == 201
    assert client.post("/api/services", json={"namespaceId": ns_a, "serviceCode": "same"}, headers=h).status_code == 409
    # 不同命名空间同 service_code 应允许
    assert client.post("/api/services", json={"namespaceId": ns_b, "serviceCode": "same"}, headers=h).status_code == 201


def test_service_get_missing_404(client: TestClient) -> None:
    h = _h(client)
    assert client.get("/api/services/999999", headers=h).status_code == 404


def test_service_requires_auth(client: TestClient) -> None:
    assert client.get("/api/services").status_code == 401
    assert client.post("/api/services", json={"namespaceId": 1, "serviceCode": "x"}).status_code == 401
