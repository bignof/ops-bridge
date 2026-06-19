"""SQLAlchemy ORM 模型(8 张台账表)。

技术栈与字段范式镜像 `service-hub/app/db_models.py`(SQLAlchemy 2.0
`Mapped`/`mapped_column`)。约束要点(评审):

- **单活不变式(M-4)**:`service_plugin_version.spv_active_key` 是 **app 维护的
  nullable unique 普通列**(active 时 = `f"{service_id}-{plugin_id}"`,否则 NULL)
  ——**禁用 MySQL 生成列**,以保证 sqlite + MySQL8 双可建。每 (service,plugin)
  至多一行 active 由该列 UNIQUE 兜底(多个 NULL 允许并存)。
- **version 不变式(M-2)**:`plugin_version.version` **NOT NULL**(= .tgz 内
  `package.json.version`)。
- 复合唯一约束:`uq_service_ns_code` / `uq_pv_plugin_version` /
  `uq_sp_service_plugin`。

DDL 真实落地见 `migrations/versions/20260619_0001_initial_schema.py`;`init_schema()`
经 alembic upgrade 建表,本模块的 `Base.metadata` 供 env.py 的 autogenerate 收集。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Namespace(Base):
    __tablename__ = "namespace"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # =agentId
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 别名
    pull_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Service(Base):
    __tablename__ = "service"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace_id: Mapped[int] = mapped_column(Integer, index=True)
    service_code: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 别名
    dir: Mapped[str | None] = mapped_column(String(2048), nullable=True)  # compose 目录(命令下发)
    default_image: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    nacos_service_name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 滚动用(新增)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("namespace_id", "service_code", name="uq_service_ns_code"),)


class Plugin(Base):
    __tablename__ = "plugin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # npm 包名
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 别名
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PluginVersion(Base):
    __tablename__ = "plugin_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[str] = mapped_column(String(255))  # NOT NULL;= package.json.version
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("plugin_id", "version", name="uq_pv_plugin_version"),)


class PluginAttachment(Base):
    __tablename__ = "plugin_attachment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_version_id: Mapped[int] = mapped_column(Integer, index=True)
    filename: Mapped[str] = mapped_column(String(512))
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_path: Mapped[str] = mapped_column(String(1024))  # 平台生成
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ServicePlugin(Base):
    __tablename__ = "service_plugin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("service_id", "plugin_id", name="uq_sp_service_plugin"),)


class ServicePluginVersion(Base):
    __tablename__ = "service_plugin_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_version_id: Mapped[int] = mapped_column(Integer, index=True)
    version_order: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_rolled_back: Mapped[bool] = mapped_column(Boolean, default=False)
    # 单活:app 维护的 nullable unique 普通列(active 时 = f"{service_id}-{plugin_id}",否则 NULL)
    spv_active_key: Mapped[str | None] = mapped_column(String(512), nullable=True, unique=True)
    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FetchRecord(Base):
    __tablename__ = "fetch_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace_id: Mapped[int] = mapped_column(Integer, index=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_version_id: Mapped[int] = mapped_column(Integer, index=True)
    fetch_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
