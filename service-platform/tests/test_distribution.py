"""分发端点测试(Task 11):queryPlugin 兼容 + id 归属式下载防 IDOR + pull token 鉴权。

经 conftest 的 `client` fixture(临时文件库 + swap 单例)。**额外**把 frozen
`settings.plugin_storage_dir` 指到 `tmp_path`(照 test_upload 的 storage_tmp 范式),
因为 download 端点要真读盘。

中间件已白名单放行 `/api/distribution/**`,故这些端点**没有 session JWT 保护**,
鉴权 100% 靠端点内 pull token。本测试覆盖 brief 8 条,尤其:
- #4 IDOR:ns A token 下载属于 ns B 的 attachmentId → 404(归属式,防探测存在性)。
- #1 version 非空。
- #7 无 Authorization → 拒;#8 格式合法但无匹配哈希 → 拒。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import store, tokens
from app.db_models import (
    Namespace,
    Plugin,
    PluginAttachment,
    PluginVersion,
    Service,
    ServicePlugin,
    ServicePluginVersion,
)


@pytest.fixture()
def storage_tmp(tmp_path: Path):
    """把 frozen settings.plugin_storage_dir / plugin_download_base_url 指到测试值,退出还原。"""
    import app.main as main_module

    old_dir = main_module.settings.plugin_storage_dir
    old_base = main_module.settings.plugin_download_base_url
    object.__setattr__(main_module.settings, "plugin_storage_dir", str(tmp_path / "plugins"))
    # download url 前缀:断言 url 字面拼接用,固定一个可识别的 base。
    object.__setattr__(main_module.settings, "plugin_download_base_url", "https://platform.example")
    yield
    object.__setattr__(main_module.settings, "plugin_storage_dir", old_dir)
    object.__setattr__(main_module.settings, "plugin_download_base_url", old_base)


def _publish_chain(ns_code: str, *, plugin_code: str, version: str, payload: bytes) -> dict:
    """造一条完整发布链:namespace(带 pull token)→ service → plugin → plugin_version
    → attachment(真实落盘)→ active service_plugin_version。

    返回 {pullToken(明文), namespaceId, serviceId, pluginId, pluginVersionId, attachmentId}。
    """
    plain, token_hash = tokens.new_pull_token()
    ns = store.create_row(Namespace, {"code": ns_code, "pull_token_hash": token_hash})
    svc = store.create_row(Service, {"namespace_id": ns.id, "service_code": f"{ns_code}-svc"})
    plugin = store.create_row(Plugin, {"code": plugin_code})
    pv = store.create_row(PluginVersion, {"plugin_id": plugin.id, "version": version})
    # 落盘真实文件(download 端点经 storage.open_stream 读),storage_path 入库相对路径。
    storage_path = _store_payload(plugin.id, pv.id, f"{plugin_code.split('/')[-1]}-{version}.tgz", payload)
    att = store.create_row(
        PluginAttachment,
        {"plugin_version_id": pv.id, "filename": f"{version}.tgz", "size": len(payload), "storage_path": storage_path},
    )
    sp = store.create_row(ServicePlugin, {"service_id": svc.id, "plugin_id": plugin.id})
    store.create_row(
        ServicePluginVersion,
        {
            "service_plugin_id": sp.id,
            "service_id": svc.id,
            "plugin_id": plugin.id,
            "plugin_version_id": pv.id,
            "version_order": 1,
            "is_active": True,
            "is_rolled_back": False,
            "spv_active_key": f"{svc.id}-{plugin.id}",
            "publish_time": datetime.now(timezone.utc),
        },
    )
    return {
        "pullToken": plain,
        "namespaceId": ns.id,
        "serviceId": svc.id,
        "pluginId": plugin.id,
        "pluginVersionId": pv.id,
        "attachmentId": att.id,
        "nsCode": ns_code,
        "serviceCode": svc.service_code,
    }


def _store_payload(plugin_id: int, version_id: int, filename: str, data: bytes) -> str:
    from app import storage

    return storage.store_tgz(plugin_id, version_id, filename, data)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- #1 / #6 query plugins: 200 + 数组形状 + version 非空 + url 拼接 -----------


def test_query_plugins_returns_array_shape(client: TestClient, storage_tmp) -> None:
    """#1 + #6:ns A token 调 plugins?namespace=A&service=... → 200,返回数组
    [{pluginName, version, url}](sync-plugins 可解析),version 非空,url 含 attachmentId。"""
    a = _publish_chain("ns-a", plugin_code="@business/plugin-x", version="1.2.3", payload=b"AAA")

    r = client.get(
        f"/api/distribution/plugins?namespace={a['nsCode']}&service={a['serviceCode']}",
        headers=_bearer(a["pullToken"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list) and len(body) == 1
    item = body[0]
    # 三字段字面(不走 to_camel 改名),与现 queryPlugin 一致。
    assert set(item.keys()) >= {"pluginName", "version", "url"}
    assert item["pluginName"] == "@business/plugin-x"
    assert item["version"] == "1.2.3" and item["version"]  # version 非空(#1)
    # url = base + /api/distribution/download/<attachmentId>
    assert item["url"].endswith(f"/api/distribution/download/{a['attachmentId']}")
    assert item["url"].startswith("https://platform.example")


# --- #2 token 不属 query 的 namespace → 403 ----------------------------------


def test_query_plugins_token_not_owning_namespace_403(client: TestClient, storage_tmp) -> None:
    """#2:ns A token 调 plugins?namespace=B → 403(token 不属 B)。"""
    a = _publish_chain("ns-a", plugin_code="@business/plugin-x", version="1.0.0", payload=b"AAA")
    b = _publish_chain("ns-b", plugin_code="@business/plugin-y", version="2.0.0", payload=b"BBB")

    r = client.get(
        f"/api/distribution/plugins?namespace={b['nsCode']}&service={b['serviceCode']}",
        headers=_bearer(a["pullToken"]),  # A 的 token,查 B
    )
    assert r.status_code == 403, r.text


# --- #3 下载本 namespace 的 attachment → 200 字节 ----------------------------


def test_download_own_attachment_200(client: TestClient, storage_tmp) -> None:
    """#3:ns A token 下载属于 A 的 attachmentId → 200,返回字节内容。"""
    a = _publish_chain("ns-a", plugin_code="@business/plugin-x", version="1.0.0", payload=b"HELLO-A")

    r = client.get(
        f"/api/distribution/download/{a['attachmentId']}",
        headers=_bearer(a["pullToken"]),
    )
    assert r.status_code == 200, r.text
    assert r.content == b"HELLO-A"


# --- #4 IDOR:A token 下载 B 的 attachment → 404(归属式,防探测) -----------


def test_download_other_namespace_attachment_404_idor(client: TestClient, storage_tmp) -> None:
    """#4(IDOR 核心):ns A token 下载属于 ns B 的 attachmentId → 404。

    **未做归属校验时此处会是 200(红)**:token 本身合法、attachment 真实存在,
    若端点只验 token 合法不验「该 attachment 是否链到 token 所属 namespace」,
    就会把 B 的包发给 A。做了归属链校验后 → 404(不是 403,防探测存在性)。
    """
    a = _publish_chain("ns-a", plugin_code="@business/plugin-x", version="1.0.0", payload=b"SECRET-A")
    b = _publish_chain("ns-b", plugin_code="@business/plugin-y", version="2.0.0", payload=b"SECRET-B")

    r = client.get(
        f"/api/distribution/download/{b['attachmentId']}",  # B 的 attachment
        headers=_bearer(a["pullToken"]),  # A 的 token
    )
    assert r.status_code == 404, r.text
    # 不应泄漏 B 的字节
    assert r.content != b"SECRET-B"


# --- #5 query 后写入一行 fetch_record ----------------------------------------


def test_query_plugins_writes_fetch_record(client: TestClient, storage_tmp) -> None:
    """#5:plugins 调用后 fetch_record 新增一行(字段对齐 ORM snake)。"""
    a = _publish_chain("ns-a", plugin_code="@business/plugin-x", version="1.0.0", payload=b"AAA")
    from app.db_models import FetchRecord

    before, _ = store.list_rows(FetchRecord, page=1, page_size=100)
    r = client.get(
        f"/api/distribution/plugins?namespace={a['nsCode']}&service={a['serviceCode']}",
        headers=_bearer(a["pullToken"]),
    )
    assert r.status_code == 200, r.text
    after, _ = store.list_rows(FetchRecord, page=1, page_size=100)
    assert len(after) == len(before) + 1
    rec = after[-1]
    # 多词字段未被 camel 键灌坏(by_alias=False 写入校验)。
    assert rec.namespace_id == a["namespaceId"]
    assert rec.service_id == a["serviceId"]
    assert rec.plugin_id == a["pluginId"]
    assert rec.plugin_version_id == a["pluginVersionId"]
    assert rec.fetch_date is not None


# --- #7 无 Authorization → 拒(纵深回归,中间件已放行 distribution) ----------


def test_no_authorization_rejected(client: TestClient, storage_tmp) -> None:
    """#7:不带 Authorization 调 plugins / download → 拒(401/403)。

    中间件白名单已放行 /api/distribution/**,故必须靠端点内 pull token 拒绝。
    """
    a = _publish_chain("ns-a", plugin_code="@business/plugin-x", version="1.0.0", payload=b"AAA")

    r1 = client.get(f"/api/distribution/plugins?namespace={a['nsCode']}&service={a['serviceCode']}")
    assert r1.status_code in (401, 403), r1.text

    r2 = client.get(f"/api/distribution/download/{a['attachmentId']}")
    assert r2.status_code in (401, 403), r2.text


# --- #8 格式合法但无任何匹配哈希的随机 token → 拒 ----------------------------


def test_bogus_token_no_matching_hash_rejected(client: TestClient, storage_tmp) -> None:
    """#8:带格式合法但无任何 pull_token_hash 匹配的随机 token → 拒(plugins 403、download 404)。

    token 解析对 None/空/无匹配先拒,绝不先 == 裸比哈希。
    """
    a = _publish_chain("ns-a", plugin_code="@business/plugin-x", version="1.0.0", payload=b"AAA")
    bogus, _ = tokens.new_pull_token()  # 合法格式,但其 hash 未入任何 namespace

    r1 = client.get(
        f"/api/distribution/plugins?namespace={a['nsCode']}&service={a['serviceCode']}",
        headers=_bearer(bogus),
    )
    assert r1.status_code == 403, r1.text

    r2 = client.get(
        f"/api/distribution/download/{a['attachmentId']}",
        headers=_bearer(bogus),
    )
    assert r2.status_code == 404, r2.text
