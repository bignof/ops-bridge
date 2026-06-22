"""镜像台账(P4-4)store + 端点测试。

经 conftest 的 `client` fixture(临时文件库 + swap 单例)。验证点:
- 建表自检(service_images 在 sqlite 上真建出)。
- store.set_current_image:首次插入置 current;再 set 另一 image → 旧的 is_current 变 False、
  新的 True(同 service 至多一 current);复用既有行不刷 created_at。
- store.set_current_image 跨 service 不互相影响(单活作用域 = 单 service)。
- store.list_service_images:created_at 倒序。
- store.add_service_image:只补历史不置 current;重复 (service, image) 去重。
- 端点 GET/POST happy(camelCase 信封 / 单行响应,无 snake key)+ 无 Bearer → 401。

直调 `store.*` 的断言同样经 fixture 换库(单例已 swap),故 store-level 用例也带 `client` 入参。
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import store
from app.db import Database
from app.db_models import Namespace, Service, ServiceImage


def _h(client: TestClient) -> dict[str, str]:
    token = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _mk_service(svc_code: str) -> int:
    """落 namespace + service,返回 service_id。"""
    ns_id = store.create_row(Namespace, {"code": f"ns-{svc_code}", "name": None}).id
    return store.create_row(Service, {"namespace_id": ns_id, "service_code": svc_code}).id


# --- 建表自检 --------------------------------------------------------------


def test_service_images_table_created(tmp_path) -> None:
    d = Database(f"sqlite:///{tmp_path}/t.db")
    d.init_schema()
    assert "service_images" in sa.inspect(d.engine).get_table_names()
    d.engine.dispose()


# --- store.set_current_image 单活 ------------------------------------------


def test_set_current_first_insert_marks_current(client: TestClient) -> None:
    sid = _mk_service("svc-img-1")
    row = store.set_current_image(sid, "registry/svc:1.0")
    assert row.service_id == sid
    assert row.image == "registry/svc:1.0"
    assert row.is_current is True
    assert row.created_at is not None

    rows = store.find_rows(ServiceImage, filters=[ServiceImage.service_id == sid])
    assert len(rows) == 1


def test_set_current_switch_flips_old_to_false(client: TestClient) -> None:
    # 再 set 另一 image:旧的 is_current 变 False、新的 True(同 service 至多一 current)。
    sid = _mk_service("svc-img-2")
    store.set_current_image(sid, "registry/svc:1.0")
    new = store.set_current_image(sid, "registry/svc:2.0")
    assert new.image == "registry/svc:2.0"
    assert new.is_current is True

    current_rows = store.find_rows(
        ServiceImage,
        filters=[ServiceImage.service_id == sid, ServiceImage.is_current.is_(True)],
    )
    assert len(current_rows) == 1  # 至多一行 current
    assert current_rows[0].image == "registry/svc:2.0"

    all_rows = store.find_rows(ServiceImage, filters=[ServiceImage.service_id == sid])
    assert len(all_rows) == 2  # 历史两行都在
    old = next(r for r in all_rows if r.image == "registry/svc:1.0")
    assert old.is_current is False


def test_set_current_reuses_existing_row_keeps_created_at(client: TestClient) -> None:
    # 把已存在的 image 再次置 current:复用同一行(不新增),且不刷 created_at。
    sid = _mk_service("svc-img-3")
    first = store.set_current_image(sid, "registry/svc:1.0")
    first_id = first.id
    first_created = first.created_at
    store.set_current_image(sid, "registry/svc:2.0")  # 切走
    # 再把 1.0 置回 current:应命中原行。
    back = store.set_current_image(sid, "registry/svc:1.0")
    assert back.id == first_id
    assert back.created_at == first_created  # 复用不刷时间
    assert back.is_current is True

    all_rows = store.find_rows(ServiceImage, filters=[ServiceImage.service_id == sid])
    assert len(all_rows) == 2  # 仍只两行(1.0 / 2.0),未因复用而新增


def test_set_current_scope_is_per_service(client: TestClient) -> None:
    # 单活作用域 = 单 service:对 service B set-current 不影响 service A 的 current。
    sid_a = _mk_service("svc-img-a")
    sid_b = _mk_service("svc-img-b")
    store.set_current_image(sid_a, "registry/a:1.0")
    store.set_current_image(sid_b, "registry/b:1.0")

    a_current = store.find_rows(
        ServiceImage,
        filters=[ServiceImage.service_id == sid_a, ServiceImage.is_current.is_(True)],
    )
    assert len(a_current) == 1 and a_current[0].image == "registry/a:1.0"


# --- store.list_service_images 倒序 ----------------------------------------


def test_list_service_images_desc_by_created_at(client: TestClient) -> None:
    sid = _mk_service("svc-img-list")
    store.set_current_image(sid, "registry/svc:1.0")
    store.set_current_image(sid, "registry/svc:2.0")
    store.set_current_image(sid, "registry/svc:3.0")

    rows = store.list_service_images(sid)
    # created_at 倒序;同刻度按 id 倒序兜底 → 最后插入的 3.0 在最前。
    assert [r.image for r in rows] == ["registry/svc:3.0", "registry/svc:2.0", "registry/svc:1.0"]


# --- store.add_service_image 只补历史 + 去重 -------------------------------


def test_add_service_image_history_only_and_dedup(client: TestClient) -> None:
    sid = _mk_service("svc-img-add")
    store.set_current_image(sid, "registry/svc:1.0")  # current
    added = store.add_service_image(sid, "registry/svc:0.9")  # 仅历史
    assert added.is_current is False

    # current 仍是 1.0(add 不动单活态)。
    current_rows = store.find_rows(
        ServiceImage,
        filters=[ServiceImage.service_id == sid, ServiceImage.is_current.is_(True)],
    )
    assert len(current_rows) == 1 and current_rows[0].image == "registry/svc:1.0"

    # 重复 add 同 (service, image):去重,不新增、原样返回。
    again = store.add_service_image(sid, "registry/svc:0.9")
    assert again.id == added.id
    all_rows = store.find_rows(ServiceImage, filters=[ServiceImage.service_id == sid])
    assert len(all_rows) == 2  # 1.0 + 0.9,无重复


# --- 端点:GET / POST happy + camelCase + 401 ------------------------------


def test_endpoint_list_and_set_current_roundtrip(client: TestClient) -> None:
    h = _h(client)
    sid = _mk_service("svc-img-ep")

    # POST set-current(camelCase body)→ 单行响应。
    r = client.post(
        f"/api/services/{sid}/images/set-current",
        json={"image": "registry/svc:1.0"},
        headers=h,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["image"] == "registry/svc:1.0"
    assert data["isCurrent"] is True
    assert data["serviceId"] == sid
    assert "createdAt" in data
    # 响应无 snake key(把 camel 契约钉死在 HTTP 层)。
    for snake in ("service_id", "is_current", "created_at"):
        assert snake not in data

    # 再切一版,验证 GET 列表信封 + 倒序 + 当前镜像。
    client.post(f"/api/services/{sid}/images/set-current", json={"image": "registry/svc:2.0"}, headers=h)
    body = client.get(f"/api/services/{sid}/images", headers=h).json()
    assert body["count"] == 2
    assert "totalPage" in body and body["page"] == 1
    assert [row["image"] for row in body["rows"]] == ["registry/svc:2.0", "registry/svc:1.0"]
    current = [row for row in body["rows"] if row["isCurrent"]]
    assert len(current) == 1 and current[0]["image"] == "registry/svc:2.0"


def test_endpoint_list_empty_envelope(client: TestClient) -> None:
    # 无任何镜像行时:count=0、rows=[]、totalPage=0(空信封不报错)。
    h = _h(client)
    sid = _mk_service("svc-img-empty")
    body = client.get(f"/api/services/{sid}/images", headers=h).json()
    assert body["count"] == 0
    assert body["rows"] == []
    assert body["totalPage"] == 0


def test_endpoint_requires_auth(client: TestClient) -> None:
    # 无 Bearer → default-deny 中间件 401(GET 与 POST 均拦)。
    assert client.get("/api/services/1/images").status_code == 401
    assert client.post("/api/services/1/images/set-current", json={"image": "x"}).status_code == 401
