"""发布/历史激活/回滚端到端测试(Task 10)。

经 conftest 的 `client` fixture(临时文件库 + swap 单例)。验证点(brief Step 1):

1) publish v10 → 唯一 active 是 v10,versionOrder=1,spvActiveKey="<sid>-<pid>"。
2) publish v11 → active=v11,v10 灭活(key=None),versionOrder=2。
3) rollback(当前 active=v11) → active 回 v10,v11 is_rolled_back=True。
4) rollback 跳过已回滚:候选谓词 versionOrder<当前 ∧ not is_rolled_back ∧ not is_active。
5) reactivate(历史 spv) → 该行 active + is_rolled_back 清 False(M-6)。
6) 并发幂等兜底:直接两行 active 插入被 DB UNIQUE 挡;publish 内部先全灭活再置活。
7) **评审 M4(最常见回滚路径,不加 flush 必撞 UNIQUE)**:当前 active = 后发布的高 PK 行,
   rollback / reactivate 到先发布的**低 PK** 历史行(现 6 条覆盖不到此爆炸路径)。
8) releases list 两视图(H4):不传 filter → 主表每绑定一行 active;传 serviceId+pluginId →
   该绑定历史;rows 经 LEFT JOIN 含 serviceCode/pluginCode/version(+namespaceCode)。

父行(namespace/service/plugin/plugin_version/service_plugin)直接经 store 落库,保持本文件
聚焦发布状态机。直调 `store.*` 的断言同样经 fixture 换库(单例已 swap)。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import store
from app.db_models import (
    Namespace,
    Plugin,
    PluginVersion,
    Service,
    ServicePlugin,
    ServicePluginVersion,
)


def _h(client: TestClient) -> dict[str, str]:
    token = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _mk_binding(ns_code: str, svc_code: str, plg_code: str) -> tuple[int, int, int]:
    """落 namespace/service/plugin + service_plugin 绑定;返回 (service_id, plugin_id, service_plugin_id)。"""
    ns_id = store.create_row(Namespace, {"code": ns_code, "name": None}).id
    svc_id = store.create_row(Service, {"namespace_id": ns_id, "service_code": svc_code}).id
    plg_id = store.create_row(Plugin, {"code": plg_code, "name": None}).id
    sp_id = store.create_row(ServicePlugin, {"service_id": svc_id, "plugin_id": plg_id}).id
    return svc_id, plg_id, sp_id


def _mk_version(plg_id: int, version: str) -> int:
    return store.create_row(PluginVersion, {"plugin_id": plg_id, "version": version, "name": None}).id


def _active_rows(svc_id: int, plg_id: int) -> list[ServicePluginVersion]:
    return store.find_rows(
        ServicePluginVersion,
        filters=[
            ServicePluginVersion.service_id == svc_id,
            ServicePluginVersion.plugin_id == plg_id,
            ServicePluginVersion.is_active.is_(True),
        ],
    )


# --- 1)+2) publish 单活 + version_order 自增 ----------------------------------


def test_publish_first_then_second_single_active(client: TestClient) -> None:
    svc_id, plg_id, _ = _mk_binding("pub-ns", "pub-svc", "pub-plg")
    pv10 = _mk_version(plg_id, "1.0")
    pv11 = _mk_version(plg_id, "1.1")

    # 1) publish v10 → 唯一 active,versionOrder=1,spvActiveKey 设值
    r1 = store.publish(svc_id, plg_id, pv10)
    assert r1.is_active is True
    assert r1.version_order == 1
    assert r1.spv_active_key == f"{svc_id}-{plg_id}"
    actives = _active_rows(svc_id, plg_id)
    assert len(actives) == 1 and actives[0].id == r1.id

    # 2) publish v11 → active=v11(order=2),v10 灭活(key=None)
    r2 = store.publish(svc_id, plg_id, pv11)
    assert r2.is_active is True and r2.version_order == 2
    actives = _active_rows(svc_id, plg_id)
    assert len(actives) == 1 and actives[0].id == r2.id
    old = store.get_row(ServicePluginVersion, r1.id)
    assert old.is_active is False and old.spv_active_key is None


# --- 3) rollback 当前 active → 回前一版,当前标记 is_rolled_back ----------------


def test_rollback_returns_to_previous_and_marks_rolled_back(client: TestClient) -> None:
    svc_id, plg_id, _ = _mk_binding("rb-ns", "rb-svc", "rb-plg")
    r10 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.0"))
    r11 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.1"))

    rolled = store.rollback(r11.id)
    assert rolled.id == r10.id and rolled.is_active is True
    assert rolled.spv_active_key == f"{svc_id}-{plg_id}"
    cur = store.get_row(ServicePluginVersion, r11.id)
    assert cur.is_active is False and cur.is_rolled_back is True and cur.spv_active_key is None
    assert len(_active_rows(svc_id, plg_id)) == 1


# --- 4) rollback 跳过已回滚的版本 ----------------------------------------------


def test_rollback_skips_already_rolled_back(client: TestClient) -> None:
    svc_id, plg_id, _ = _mk_binding("skip-ns", "skip-svc", "skip-plg")
    r10 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.0"))
    r11 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.1"))
    r12 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.2"))

    # 回滚 v12 → v11(候选);v11 现 active
    assert store.rollback(r12.id).id == r11.id
    # 再回滚 v11 → 跳过已回滚的 v12,落到 v10(候选谓词排除 is_rolled_back)
    assert store.rollback(r11.id).id == r10.id
    v12 = store.get_row(ServicePluginVersion, r12.id)
    assert v12.is_rolled_back is True and v12.is_active is False
    assert len(_active_rows(svc_id, plg_id)) == 1


def test_rollback_no_candidate_raises_not_found(client: TestClient) -> None:
    svc_id, plg_id, _ = _mk_binding("noc-ns", "noc-svc", "noc-plg")
    r10 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.0"))
    # 仅一版,无更早候选 → NotFound
    with pytest.raises(store.NotFound):
        store.rollback(r10.id)


def test_rollback_non_active_raises_conflict(client: TestClient) -> None:
    svc_id, plg_id, _ = _mk_binding("na-ns", "na-svc", "na-plg")
    r10 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.0"))
    store.publish(svc_id, plg_id, _mk_version(plg_id, "1.1"))  # v10 已非 active
    with pytest.raises(store.Conflict):
        store.rollback(r10.id)


# --- 5) reactivate 历史版本 + 清 is_rolled_back(M-6)--------------------------


def test_reactivate_history_clears_rolled_back(client: TestClient) -> None:
    svc_id, plg_id, _ = _mk_binding("re-ns", "re-svc", "re-plg")
    r10 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.0"))
    r11 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.1"))
    # 先回滚使 v11 is_rolled_back=True
    store.rollback(r11.id)
    assert store.get_row(ServicePluginVersion, r11.id).is_rolled_back is True

    # reactivate v11 → active + is_rolled_back 清 False(M-6),v10 灭活
    again = store.reactivate(r11.id)
    assert again.id == r11.id and again.is_active is True
    assert again.is_rolled_back is False
    assert again.spv_active_key == f"{svc_id}-{plg_id}"
    assert store.get_row(ServicePluginVersion, r10.id).is_active is False
    assert len(_active_rows(svc_id, plg_id)) == 1


def test_reactivate_missing_raises_not_found(client: TestClient) -> None:
    _mk_binding("rem-ns", "rem-svc", "rem-plg")
    with pytest.raises(store.NotFound):
        store.reactivate(999999)


# --- 6) 并发幂等兜底:两行 active 被 DB UNIQUE 挡 -----------------------------


def test_two_active_rows_blocked_by_unique(client: TestClient) -> None:
    # spv_active_key UNIQUE:同 (service,plugin) 第二行带相同 key 直接撞 IntegrityError → Conflict。
    svc_id, plg_id, sp_id = _mk_binding("uq-ns", "uq-svc", "uq-plg")
    pv = _mk_version(plg_id, "1.0")
    key = f"{svc_id}-{plg_id}"
    store.create_row(
        ServicePluginVersion,
        {
            "service_plugin_id": sp_id,
            "service_id": svc_id,
            "plugin_id": plg_id,
            "plugin_version_id": pv,
            "version_order": 1,
            "is_active": True,
            "is_rolled_back": False,
            "spv_active_key": key,
        },
    )
    with pytest.raises(store.Conflict):
        store.create_row(
            ServicePluginVersion,
            {
                "service_plugin_id": sp_id,
                "service_id": svc_id,
                "plugin_id": plg_id,
                "plugin_version_id": pv,
                "version_order": 2,
                "is_active": True,
                "is_rolled_back": False,
                "spv_active_key": key,  # 同 key → UNIQUE 违例
            },
        )


def test_publish_unbound_raises_not_found(client: TestClient) -> None:
    # 未建 service_plugin 绑定即 publish → NotFound。
    with pytest.raises(store.NotFound):
        store.publish(123456, 654321, 1)


def test_publish_cross_plugin_version_raises_not_found(client: TestClient) -> None:
    """最终评审修复:plugin_version_id 必须归属于 plugin_id。

    建插件 A(版本 va)、插件 B(版本 vb);对「绑定了插件 A 的 service」publish 传 vb
    (B 的版本)→ 期望 NotFound(路由层 → 404),拒绝跨插件错配写进台账。
    并保留一条正常 publish(传本插件 A 的版本 va)仍成功的对照。
    """
    svc_a, plg_a, _ = _mk_binding("xpv-ns", "xpv-svc-a", "xpv-plg-a")
    plg_b = store.create_row(Plugin, {"code": "xpv-plg-b", "name": None}).id
    va = _mk_version(plg_a, "1.0")  # 属于插件 A
    vb = _mk_version(plg_b, "9.9")  # 属于插件 B

    # 跨插件错配:绑定的是 A,却传 B 的版本 → NotFound(版本不归属本插件)
    with pytest.raises(store.NotFound):
        store.publish(svc_a, plg_a, vb)

    # 对照:传本插件 A 的版本 → 正常成功
    ok = store.publish(svc_a, plg_a, va)
    assert ok.is_active is True and ok.plugin_version_id == va


def test_publish_endpoint_cross_plugin_version_404(client: TestClient) -> None:
    """端点层:跨插件错配版本 → 404;同绑定传本插件版本 → 201(对照)。"""
    h = _h(client)
    svc_a, plg_a, _ = _mk_binding("xpv2-ns", "xpv2-svc-a", "xpv2-plg-a")
    plg_b = store.create_row(Plugin, {"code": "xpv2-plg-b", "name": None}).id
    va = _mk_version(plg_a, "1.0")
    vb = _mk_version(plg_b, "9.9")

    bad = client.post(
        "/api/releases/publish",
        json={"serviceId": svc_a, "pluginId": plg_a, "pluginVersionId": vb},
        headers=h,
    )
    assert bad.status_code == 404, bad.text

    good = client.post(
        "/api/releases/publish",
        json={"serviceId": svc_a, "pluginId": plg_a, "pluginVersionId": va},
        headers=h,
    )
    assert good.status_code == 201, good.text


# --- 7) 评审 M4:当前 active = 高 PK,回滚/激活到低 PK 历史行(不加 flush 必撞 UNIQUE)---


def test_rollback_high_pk_to_low_pk_no_unique_violation(client: TestClient) -> None:
    """评审 M4 复现路径:publish(v10)→publish(v11)→rollback() 回到 v10。

    v11 是后发布的**高 PK** 当前 active,v10 是先发布的**低 PK** 历史行。store 三函数
    在「清所有 key」与「置目标 key」之间 `s.flush()`;**不加 flush** 时 SQLAlchemy 按主键
    升序发 UPDATE,低 PK(v10)的「置 key」会先于高 PK(v11)的「清 key」发出 → UNIQUE
    立即违例。本用例断言:不撞 UNIQUE、active 正确切回低 PK 行、全局仍唯一 active。
    """
    svc_id, plg_id, _ = _mk_binding("m4rb-ns", "m4rb-svc", "m4rb-plg")
    r10 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.0"))  # 低 PK
    r11 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.1"))  # 高 PK,当前 active
    assert r11.id > r10.id  # 钉死 PK 顺序前提(若实现改用非自增 PK 则失效需重审)

    # 关键:不加 flush 此调用会抛 IntegrityError(UNIQUE constraint failed: spv_active_key)
    rolled = store.rollback(r11.id)
    assert rolled.id == r10.id and rolled.is_active is True
    assert rolled.spv_active_key == f"{svc_id}-{plg_id}"
    assert store.get_row(ServicePluginVersion, r11.id).is_rolled_back is True
    assert len(_active_rows(svc_id, plg_id)) == 1


def test_reactivate_high_pk_to_low_pk_no_unique_violation(client: TestClient) -> None:
    """评审 M4 同源路径:reactivate 到先发布的低 PK 历史行。

    当前 active 为高 PK v11,reactivate 低 PK v10:同样需 flush 分隔,否则低 PK 的置 key
    先于高 PK 的清 key → UNIQUE 违例。
    """
    svc_id, plg_id, _ = _mk_binding("m4re-ns", "m4re-svc", "m4re-plg")
    r10 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.0"))  # 低 PK
    r11 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.1"))  # 高 PK,当前 active
    assert r11.id > r10.id

    again = store.reactivate(r10.id)
    assert again.id == r10.id and again.is_active is True
    assert again.spv_active_key == f"{svc_id}-{plg_id}"
    assert store.get_row(ServicePluginVersion, r11.id).is_active is False
    assert len(_active_rows(svc_id, plg_id)) == 1


# --- 8) H4 releases list 两视图 ----------------------------------------------


def test_releases_list_main_view_one_active_per_binding(client: TestClient) -> None:
    """不传 filter → 主表:每 (service,plugin) 一行 active;rows 含 serviceCode/pluginCode/version。"""
    h = _h(client)
    svc_a, plg_a, _ = _mk_binding("h4a-ns", "h4a-svc", "h4a-plg")
    svc_b, plg_b, _ = _mk_binding("h4b-ns", "h4b-svc", "h4b-plg")
    store.publish(svc_a, plg_a, _mk_version(plg_a, "1.0"))
    store.publish(svc_a, plg_a, _mk_version(plg_a, "1.1"))  # 绑定 A 现有 1 行 active(v1.1)
    store.publish(svc_b, plg_b, _mk_version(plg_b, "2.0"))  # 绑定 B 1 行 active

    body = client.get("/api/releases", headers=h).json()
    assert "totalPage" in body and body["page"] == 1 and body["pageSize"] == 20
    # 主表:每绑定恰一行 active
    a_rows = [r for r in body["rows"] if r["serviceId"] == svc_a and r["pluginId"] == plg_a]
    b_rows = [r for r in body["rows"] if r["serviceId"] == svc_b and r["pluginId"] == plg_b]
    assert len(a_rows) == 1 and len(b_rows) == 1
    assert all(r["isActive"] is True for r in body["rows"])
    # LEFT JOIN 名称列(评审 H3)
    a = a_rows[0]
    assert a["serviceCode"] == "h4a-svc" and a["pluginCode"] == "h4a-plg"
    assert a["namespaceCode"] == "h4a-ns" and a["version"] == "1.1"


def test_releases_list_is_active_filter_equals_main_view(client: TestClient) -> None:
    # isActive=true 与「不传 filter」语义一致 → 主表。
    h = _h(client)
    svc_id, plg_id, _ = _mk_binding("h4f-ns", "h4f-svc", "h4f-plg")
    store.publish(svc_id, plg_id, _mk_version(plg_id, "1.0"))
    store.publish(svc_id, plg_id, _mk_version(plg_id, "1.1"))

    body = client.get("/api/releases?isActive=true", headers=h).json()
    rows = [r for r in body["rows"] if r["serviceId"] == svc_id and r["pluginId"] == plg_id]
    assert len(rows) == 1 and rows[0]["isActive"] is True and rows[0]["version"] == "1.1"


def test_releases_list_history_view_by_service_and_plugin(client: TestClient) -> None:
    """传 serviceId+pluginId → 该绑定版本历史(全部版本,versionOrder 升序)。"""
    h = _h(client)
    svc_id, plg_id, _ = _mk_binding("h4h-ns", "h4h-svc", "h4h-plg")
    store.publish(svc_id, plg_id, _mk_version(plg_id, "1.0"))
    store.publish(svc_id, plg_id, _mk_version(plg_id, "1.1"))
    store.publish(svc_id, plg_id, _mk_version(plg_id, "1.2"))

    body = client.get(f"/api/releases?serviceId={svc_id}&pluginId={plg_id}", headers=h).json()
    assert body["count"] == 3
    orders = [r["versionOrder"] for r in body["rows"]]
    assert orders == [1, 2, 3]  # 升序
    versions = [r["version"] for r in body["rows"]]
    assert versions == ["1.0", "1.1", "1.2"]
    # 历史视图含已灭活行(仅最新 active)
    assert sum(1 for r in body["rows"] if r["isActive"]) == 1
    assert all(r["pluginCode"] == "h4h-plg" for r in body["rows"])


# --- 端点:错误映射 + 鉴权 ---------------------------------------------------


def test_publish_endpoint_unbound_404(client: TestClient) -> None:
    h = _h(client)
    r = client.post(
        "/api/releases/publish",
        json={"serviceId": 999, "pluginId": 999, "pluginVersionId": 1},
        headers=h,
    )
    assert r.status_code == 404


def test_rollback_endpoint_non_active_409(client: TestClient) -> None:
    h = _h(client)
    svc_id, plg_id, _ = _mk_binding("ep-ns", "ep-svc", "ep-plg")
    r10 = store.publish(svc_id, plg_id, _mk_version(plg_id, "1.0"))
    store.publish(svc_id, plg_id, _mk_version(plg_id, "1.1"))
    r = client.post("/api/releases/rollback", json={"spvId": r10.id}, headers=h)
    assert r.status_code == 409


def test_releases_endpoints_require_auth(client: TestClient) -> None:
    assert client.get("/api/releases").status_code == 401
    assert client.post("/api/releases/publish", json={"serviceId": 1, "pluginId": 1, "pluginVersionId": 1}).status_code == 401
    assert client.post("/api/releases/reactivate", json={"spvId": 1}).status_code == 401
    assert client.post("/api/releases/rollback", json={"spvId": 1}).status_code == 401


def test_publish_endpoint_camel_roundtrip_no_snake_keys(client: TestClient) -> None:
    # 评审 H2:camelCase 往返 + 响应无 snake key。
    h = _h(client)
    svc_id, plg_id, sp_id = _mk_binding("camel-ns", "camel-svc", "camel-plg")
    pv = _mk_version(plg_id, "1.0")
    r = client.post(
        "/api/releases/publish",
        json={"serviceId": svc_id, "pluginId": plg_id, "pluginVersionId": pv},
        headers=h,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["serviceId"] == svc_id and data["pluginId"] == plg_id
    assert data["versionOrder"] == 1 and data["isActive"] is True
    assert data["spvActiveKey"] == f"{svc_id}-{plg_id}"
    for snake in ("service_id", "plugin_id", "version_order", "is_active", "is_rolled_back", "spv_active_key"):
        assert snake not in data
