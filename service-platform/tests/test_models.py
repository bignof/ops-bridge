"""数据模型 + 真 DB 约束测试(Task 2)。

本文件是 service-platform 首个真正建表读写的测试:不经 `client` fixture,
直接 `Database("sqlite:///<tmp_path>/t.db")` + `init_schema()`(=alembic
upgrade head,会跑本任务新写的 20260619_0001 迁移)建库,再用真 session 验
DB 层约束。验证点:

- 唯一约束(namespace.code / uq_service_ns_code / uq_pv_plugin_version /
  uq_sp_service_plugin)在 sqlite 上真生效;
- **单活不变式**:`spv_active_key`(app 维护的 nullable unique 普通列,
  非 MySQL 生成列)——同一非空 key 两行冲突(同 (service,plugin) 不能两行
  active),多个 NULL(inactive)允许并存;
- **version NOT NULL**(评审 M-2):`plugin_version.version` 缺省/置 NULL 报错;
- **跨用例互不串库**(行为性):每个用例独立 tmp_path 文件库,两用例写同一
  natural key 不互相冲突。
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app import db_models as m
from app.db import Database


def _db(tmp_path):
    d = Database(f"sqlite:///{tmp_path}/t.db")
    d.init_schema()
    return d


# --- 唯一约束 ---------------------------------------------------------------


def test_unique_namespace_code(tmp_path):
    d = _db(tmp_path)
    now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add(m.Namespace(code="ns1", created_at=now, updated_at=now))
        s.commit()
    with d.session_factory() as s:
        s.add(m.Namespace(code="ns1", created_at=now, updated_at=now))
        with pytest.raises(IntegrityError):
            s.commit()


def test_unique_plugin_code(tmp_path):
    d = _db(tmp_path)
    now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add(m.Plugin(code="@orchisky/plugin-foo", created_at=now, updated_at=now))
        s.commit()
    with d.session_factory() as s:
        s.add(m.Plugin(code="@orchisky/plugin-foo", created_at=now, updated_at=now))
        with pytest.raises(IntegrityError):
            s.commit()


def test_uq_service_ns_code(tmp_path):
    d = _db(tmp_path)
    now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add(m.Service(namespace_id=1, service_code="svc", created_at=now, updated_at=now))
        s.commit()
    # 同 namespace 下同 service_code 冲突
    with d.session_factory() as s:
        s.add(m.Service(namespace_id=1, service_code="svc", created_at=now, updated_at=now))
        with pytest.raises(IntegrityError):
            s.commit()
    # 不同 namespace 下同 service_code 允许
    with d.session_factory() as s:
        s.add(m.Service(namespace_id=2, service_code="svc", created_at=now, updated_at=now))
        s.commit()


def test_uq_pv_plugin_version(tmp_path):
    d = _db(tmp_path)
    now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add(m.PluginVersion(plugin_id=1, version="1.0.0", created_at=now, updated_at=now))
        s.commit()
    with d.session_factory() as s:
        s.add(m.PluginVersion(plugin_id=1, version="1.0.0", created_at=now, updated_at=now))
        with pytest.raises(IntegrityError):
            s.commit()


def test_uq_sp_service_plugin(tmp_path):
    d = _db(tmp_path)
    now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add(m.ServicePlugin(service_id=1, plugin_id=2, created_at=now))
        s.commit()
    with d.session_factory() as s:
        s.add(m.ServicePlugin(service_id=1, plugin_id=2, created_at=now))
        with pytest.raises(IntegrityError):
            s.commit()


# --- 单活不变式(spv_active_key nullable unique) ----------------------------


def test_spv_single_active_unique(tmp_path):
    d = _db(tmp_path)
    now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add(
            m.ServicePluginVersion(
                service_plugin_id=1,
                service_id=1,
                plugin_id=2,
                plugin_version_id=10,
                version_order=1,
                is_active=True,
                spv_active_key="1-2",
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()
    with d.session_factory() as s:
        s.add(
            m.ServicePluginVersion(
                service_plugin_id=1,
                service_id=1,
                plugin_id=2,
                plugin_version_id=11,
                version_order=2,
                is_active=True,
                spv_active_key="1-2",
                created_at=now,
                updated_at=now,
            )
        )
        with pytest.raises(IntegrityError):  # 同 (service,plugin) 不能两行 active
            s.commit()


def test_spv_multiple_inactive_ok(tmp_path):
    d = _db(tmp_path)
    now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add_all(
            [
                m.ServicePluginVersion(
                    service_plugin_id=1,
                    service_id=1,
                    plugin_id=2,
                    plugin_version_id=10,
                    version_order=1,
                    is_active=False,
                    spv_active_key=None,
                    created_at=now,
                    updated_at=now,
                ),
                m.ServicePluginVersion(
                    service_plugin_id=1,
                    service_id=1,
                    plugin_id=2,
                    plugin_version_id=11,
                    version_order=2,
                    is_active=False,
                    spv_active_key=None,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        s.commit()  # 多个 NULL active_key 允许


# --- version NOT NULL(评审 M-2) -------------------------------------------


def test_plugin_version_version_not_null(tmp_path):
    d = _db(tmp_path)
    now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add(m.PluginVersion(plugin_id=1, version=None, created_at=now, updated_at=now))
        with pytest.raises(IntegrityError):
            s.commit()


# --- 行为性:跨用例互不串库 -------------------------------------------------


def test_isolation_first(tmp_path):
    d = _db(tmp_path)
    now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add(m.Namespace(code="shared-code", created_at=now, updated_at=now))
        s.commit()
    with d.session_factory() as s:
        assert s.query(m.Namespace).filter_by(code="shared-code").count() == 1


def test_isolation_second(tmp_path):
    # 与 test_isolation_first 写同一 natural key;若串库则唯一约束会让本用例 commit 失败。
    d = _db(tmp_path)
    now = datetime.now(timezone.utc)
    with d.session_factory() as s:
        s.add(m.Namespace(code="shared-code", created_at=now, updated_at=now))
        s.commit()
    with d.session_factory() as s:
        assert s.query(m.Namespace).filter_by(code="shared-code").count() == 1
