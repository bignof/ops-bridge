"""namespace 资源端到端 CRUD 测试(Task 6b)。

经 conftest 的 `client` fixture(临时文件库 + swap 单例 + hub_state 接线 + admin_token 测试值)。验证点:
- 增删改查全链路;响应**全 camelCase**(经 `*Out` 序列化)。
- 列表信封形状 `{count, rows, page, pageSize, totalPage}`;`name` 空回退 `code`(评审 H3)。
- **create show-once(评审 H7 / Nit-1)**:hub `provision_agent` 多以 **async 桩**隔离(S5 后 hub_client.*
  是 async);响应含一次性 `agentKey` 明文;**重查行不含 agentKey 明文**(不入库)。亦含一条**不打桩**
  真跑进程内 provision 的端到端用例(见 test_namespace_create_real_inprocess_provision_then_conflict)。
- 唯一约束冲突(`namespace.code` 重复)→ 409。
- `/api/namespaces` 在 default-deny 中间件下,无 Bearer → 401。
"""

from __future__ import annotations

import app.hub_client as hc
import pytest
from app import store
from app.db_models import Namespace
from fastapi.testclient import TestClient


def _h(client: TestClient) -> dict[str, str]:
    """登录拿 JWT,组装 Authorization 头。"""
    token = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _async_return(value):
    """造一个 async 替身:被 await 时返回 value(S5 后 hub_client.* 是 async,桩须返回 coroutine)。"""

    async def _stub(*a, **k):
        return value

    return _stub


def _async_raise(exc: Exception):
    """造一个 async 替身:被 await 时抛 exc(S5 后 hub_client.* 是 async)。"""

    async def _stub(*a, **k):
        raise exc

    return _stub


def test_namespace_create_provisions_and_shows_key_once(client: TestClient, monkeypatch) -> None:
    # 评审 H7:不打桩则真跑进程内 provision(此处打桩隔离 hub),断言 201 + show-once。
    monkeypatch.setattr(hc, "provision_agent", _async_return("fake-key"))
    h = _h(client)

    r = client.post("/api/namespaces", json={"code": "ns1", "name": "NS1"}, headers=h)
    assert r.status_code == 201
    assert r.json()["agentKey"] == "fake-key"  # show-once 返回明文
    assert r.json()["code"] == "ns1"
    assert r.json()["name"] == "NS1"
    nid = r.json()["id"]

    # 评审 Nit-1:重查该行,断言不含 agentKey 明文(库内无该列/不落地,守 show-once 不变式)。
    got = client.get(f"/api/namespaces/{nid}", headers=h).json()
    assert "agentKey" not in got and "fake-key" not in str(got)


def test_namespace_create_passes_code_to_provision(client: TestClient, monkeypatch) -> None:
    # 断言 provision_agent 收到的是 code(=agentId),非别名 name。
    seen: dict = {}

    async def fake_provision(code: str) -> str:
        seen["code"] = code
        return "k-abc"

    monkeypatch.setattr(hc, "provision_agent", fake_provision)
    h = _h(client)
    r = client.post("/api/namespaces", json={"code": "agent-x", "name": "别名"}, headers=h)
    assert r.status_code == 201
    assert seen["code"] == "agent-x"


def test_namespace_crud(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(hc, "provision_agent", _async_return("fake-key"))
    h = _h(client)

    r = client.post("/api/namespaces", json={"code": "ns-crud", "name": "N"}, headers=h)
    assert r.status_code == 201
    nid = r.json()["id"]

    # list → 信封形状 + 分页字段
    body = client.get("/api/namespaces", headers=h).json()
    assert body["count"] >= 1
    assert "rows" in body and "totalPage" in body
    assert body["page"] == 1 and body["pageSize"] == 20
    assert any(row["id"] == nid for row in body["rows"])

    # get 单条
    got = client.get(f"/api/namespaces/{nid}", headers=h)
    assert got.status_code == 200
    assert got.json()["code"] == "ns-crud"

    # patch 局部更新
    patched = client.patch(f"/api/namespaces/{nid}", json={"name": "N2"}, headers=h)
    assert patched.status_code == 200
    assert patched.json()["name"] == "N2"
    assert patched.json()["code"] == "ns-crud"  # 未传字段保持原值

    # delete → 204
    assert client.delete(f"/api/namespaces/{nid}", headers=h).status_code == 204
    assert client.get(f"/api/namespaces/{nid}", headers=h).status_code == 404


def test_namespace_name_falls_back_to_code(client: TestClient, monkeypatch) -> None:
    # 评审 H3:name 空时列表/查询回退 code。
    monkeypatch.setattr(hc, "provision_agent", _async_return("k"))
    h = _h(client)
    r = client.post("/api/namespaces", json={"code": "only-code"}, headers=h)
    assert r.status_code == 201
    nid = r.json()["id"]
    assert r.json()["name"] == "only-code"  # create 响应即回退
    got = client.get(f"/api/namespaces/{nid}", headers=h).json()
    assert got["name"] == "only-code"


def test_namespace_unique_conflict(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(hc, "provision_agent", _async_return("k"))
    h = _h(client)
    first = client.post("/api/namespaces", json={"code": "dup-ns"}, headers=h)
    assert first.status_code == 201
    # 同 code 再 create → 唯一约束 → 409
    assert client.post("/api/namespaces", json={"code": "dup-ns"}, headers=h).status_code == 409


def test_namespace_get_missing_404(client: TestClient) -> None:
    h = _h(client)
    assert client.get("/api/namespaces/999999", headers=h).status_code == 404


def test_namespace_requires_auth(client: TestClient) -> None:
    # 无 Bearer → default-deny 中间件 401
    assert client.get("/api/namespaces").status_code == 401
    assert client.post("/api/namespaces", json={"code": "no-auth"}).status_code == 401


@pytest.mark.parametrize(
    "bad_code",
    [
        "x/../../dispatch",  # 路径穿越(评审 A3 实测打到 hub dispatch)
        "ns/with/slash",
        "ns#frag",
        "ns?query=1",
        "..",
        "ns with space",
        "",  # 空串
    ],
)
def test_namespace_create_rejects_illegal_code(client: TestClient, monkeypatch, bad_code: str) -> None:
    # 评审 A3:NamespaceIn.code 白名单 ^[A-Za-z0-9._-]{1,255}$;非法 code create 即被拒(422),
    # 且**不落库**(校验在入 store 之前,杜绝路径注入的 namespace.code 进 hub URL)。
    # 打桩 provision 以隔离 hub:若校验未生效而误落库,provision 桩会让它 201,从而暴露漏网。
    calls: list[str] = []

    async def fake_provision(code: str) -> str:
        calls.append(code)
        return "k"

    monkeypatch.setattr(hc, "provision_agent", fake_provision)
    h = _h(client)

    r = client.post("/api/namespaces", json={"code": bad_code}, headers=h)
    assert r.status_code == 422, r.text  # Pydantic 校验失败 → 422
    assert calls == []  # 非法 code 在校验阶段即拒,不触达 provision
    # 不落库:库内无该 code 行
    rows, _ = store.list_rows(Namespace, page=1, page_size=200)
    assert all(row.code != bad_code for row in rows)


def test_namespace_create_accepts_legal_code(client: TestClient, monkeypatch) -> None:
    # 白名单放行合法 code(含点/下划线/连字符,契合 @namespace/plugin 之外的纯 code 形态)。
    monkeypatch.setattr(hc, "provision_agent", _async_return("k"))
    h = _h(client)
    r = client.post("/api/namespaces", json={"code": "ns.legal_code-1"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["code"] == "ns.legal_code-1"


# --- R2(复审):PATCH 端的 NamespaceUpdate.code 也须套白名单(纵深不退化为单闸) ---
#    A3 白名单原只加在 create 端 NamespaceIn;PATCH 用的 NamespaceUpdate.code 无校验,
#    PATCH code='x/../../dispatch' 会 200 落库,绕过「非法 code 永不入库」第一道闸。
#    修复:把 code 白名单抽成共享函数,给 NamespaceUpdate.code 也挂校验(None 放行)。


@pytest.mark.parametrize(
    "bad_code",
    ["x/../../dispatch", "ns/with/slash", "ns#frag", "ns?query=1", "..", ".", "ns with space", ""],
)
def test_namespace_patch_rejects_illegal_code(client: TestClient, monkeypatch, bad_code: str) -> None:
    # 先建一个合法 namespace,再 PATCH 非法 code → 422 且**不落库**(code 仍是原合法值)。
    monkeypatch.setattr(hc, "provision_agent", _async_return("k"))
    h = _h(client)
    created = client.post("/api/namespaces", json={"code": "patch-legal"}, headers=h)
    assert created.status_code == 201, created.text
    nid = created.json()["id"]

    r = client.patch(f"/api/namespaces/{nid}", json={"code": bad_code}, headers=h)
    assert r.status_code == 422, r.text  # Pydantic 校验失败 → 422

    # 不落库:该行 code 未被改成非法值,仍是原合法值。
    got = client.get(f"/api/namespaces/{nid}", headers=h).json()
    assert got["code"] == "patch-legal"
    rows, _ = store.list_rows(Namespace, page=1, page_size=200)
    assert all(row.code != bad_code for row in rows)


def test_namespace_patch_accepts_legal_code(client: TestClient, monkeypatch) -> None:
    # PATCH 合法 code → 200 且落库(None 放行不影响,仅非法非 None 才拒)。
    monkeypatch.setattr(hc, "provision_agent", _async_return("k"))
    h = _h(client)
    nid = client.post("/api/namespaces", json={"code": "patch-old"}, headers=h).json()["id"]

    r = client.patch(f"/api/namespaces/{nid}", json={"code": "patch.new_code-2"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "patch.new_code-2"

    # 仅传 name(不传 code)→ None 放行,不报错。
    r2 = client.patch(f"/api/namespaces/{nid}", json={"name": "改个名"}, headers=h)
    assert r2.status_code == 200, r2.text
    assert r2.json()["code"] == "patch.new_code-2"  # code 保持原值


def test_namespace_create_rolls_back_on_hub_failure(client: TestClient, monkeypatch) -> None:
    # 评审 A14:create 先落库再 provision;hub 失败必须**补偿删除**刚建的行(整体原子失败),
    # 否则遗留无 agentKey 的孤儿 namespace(show-once 永久丢)。
    # 断言:provision 抛 HubError → 端点 5xx **且** namespace 行数为 0(无孤儿)。
    before, before_count = store.list_rows(Namespace, page=1, page_size=200)
    assert before_count == 0

    monkeypatch.setattr(hc, "provision_agent", _async_raise(hc.HubError("hub 模拟不可达")))
    h = _h(client)
    r = client.post("/api/namespaces", json={"code": "orphan-ns"}, headers=h)
    assert r.status_code >= 500, r.text  # 整体失败(A13 映射为 502)

    after, after_count = store.list_rows(Namespace, page=1, page_size=200)
    assert after_count == 0  # 补偿删除生效:无孤儿 namespace 行残留


def test_namespace_create_hub_missing_key_5xx_no_orphan(client: TestClient, monkeypatch) -> None:
    """S5(原 R3 进程内版):进程内 provision 拿不到 agentKey → create 5xx **且无孤儿 namespace**。

    跨进程时代 R3 测的是「hub 返 200 但 body 非 dict/非 JSON」的 httpx 响应解析缝;进程内化后没有
    httpx 响应可畸形,等价的失败面是「hub 逻辑层返回但缺 agentKey」。`hub_client.provision_agent`
    把这种情形归一化为 `HubError`(见其实现),路由层据此 A13 映射 502 + A14 补偿删除 → 行数=0。

    变异验证:去掉 hub_client provision 的「缺 key → HubError」归一化(直接 return 空 key),本用例
    会拿到空字符串 agentKey → 201(而非 5xx)→ 红。
    """
    before, before_count = store.list_rows(Namespace, page=1, page_size=200)
    assert before_count == 0

    monkeypatch.setattr(hc, "provision_agent", _async_raise(hc.HubError("hub 未返回 agentKey")))
    r = client.post("/api/namespaces", json={"code": "no-key-ns"}, headers=_h(client))
    assert r.status_code >= 500, r.text  # 归一化 HubError → A13 映射 502

    after, after_count = store.list_rows(Namespace, page=1, page_size=200)
    assert after_count == 0, "hub 缺 agentKey 未触发补偿删除,残留孤儿 namespace"


def test_namespace_create_real_inprocess_provision_then_conflict(client: TestClient) -> None:
    """S5 端到端:**不打桩**,真跑进程内 provision —— 首建 201、同 code 再建(hub agent 已存在)5xx 无孤儿。

    进程内化后 provision 真正落 hub 的 `agents` 表(经 conftest swap 到临时库的 `hub_state`)。首建成功
    返回 agentKey;但平台台账行被 delete 后,hub 侧 agent 仍在 → 同 code 二次 create 时 hub provision
    handler 抛 409(agent already exists),`hub_client.provision_agent` 归一化为 HubError → 路由层
    502 + 补偿删除 → 台账无孤儿。一并验证进程内直调链路真实可用(无桩)。
    """
    h = _h(client)
    # 首建:真跑进程内 provision,201 + show-once agentKey。
    r1 = client.post("/api/namespaces", json={"code": "real-prov"}, headers=h)
    assert r1.status_code == 201, r1.text
    assert r1.json()["agentKey"]  # 真实 hub 签发的密钥(非空)
    nid = r1.json()["id"]
    # 删平台台账行(hub 侧 agent 记录不随之删 → 制造 hub 已存在、台账不存在的状态)。
    assert client.delete(f"/api/namespaces/{nid}", headers=h).status_code == 204

    # 同 code 再建:平台行可建,但 hub provision 撞 409 → HubError → 502 + 补偿删除。
    r2 = client.post("/api/namespaces", json={"code": "real-prov"}, headers=h)
    assert r2.status_code >= 500, r2.text
    rows, count = store.list_rows(Namespace, page=1, page_size=200)
    assert count == 0, "hub 已存在导致 provision 失败时,平台台账须补偿删除,无孤儿"
    # 补偿删除照样生效:传输层失败同样不留孤儿行
    rows, _ = store.list_rows(Namespace, page=1, page_size=200)
    assert all(row.code != "ns-503" for row in rows)
