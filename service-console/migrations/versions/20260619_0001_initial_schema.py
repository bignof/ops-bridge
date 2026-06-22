"""initial schema (service-platform 8 张台账表)

Revision ID: 20260619_0001
Revises:
Create Date: 2026-06-19 00:00:00

8 表 + 复合唯一约束(uq_service_ns_code / uq_pv_plugin_version /
uq_sp_service_plugin)+ 单活 nullable unique 列 `spv_active_key` + 各 index;
`plugin_version.version` NOT NULL(评审 M-2)。所有 DDL 须 sqlite + MySQL8 双可建
——故单活用普通 nullable unique 列(非 MySQL 生成列,评审 M-4)。

照 `service-hub/migrations/versions/20260306_0001_initial_schema.py` 的
`op.create_table` 范式手写(非 autogenerate)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260619_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "namespace",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("pull_token_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index(op.f("ix_namespace_code"), "namespace", ["code"], unique=True)

    op.create_table(
        "service",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("namespace_id", sa.Integer(), nullable=False),
        sa.Column("service_code", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("dir", sa.String(length=2048), nullable=True),
        sa.Column("default_image", sa.String(length=2048), nullable=True),
        sa.Column("nacos_service_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("namespace_id", "service_code", name="uq_service_ns_code"),
    )
    op.create_index(op.f("ix_service_namespace_id"), "service", ["namespace_id"], unique=False)

    op.create_table(
        "plugin",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index(op.f("ix_plugin_code"), "plugin", ["code"], unique=True)

    op.create_table(
        "plugin_version",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plugin_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(length=255), nullable=False),  # NOT NULL(评审 M-2)
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plugin_id", "version", name="uq_pv_plugin_version"),
    )
    op.create_index(op.f("ix_plugin_version_plugin_id"), "plugin_version", ["plugin_id"], unique=False)

    op.create_table(
        "plugin_attachment",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plugin_version_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 评审 B1(方案 B):每 plugin_version 至多一附件(version↔.tgz 一一对应),
        # UNIQUE 使下载授权(version 粒度)与清单 func.max(attachment.id) 投放口径一致。
        # UNIQUE 自带唯一索引,故不再叠加普通 index。
        sa.UniqueConstraint("plugin_version_id", name="uq_pa_plugin_version"),
    )

    op.create_table(
        "service_plugin",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_id", "plugin_id", name="uq_sp_service_plugin"),
    )
    op.create_index(op.f("ix_service_plugin_service_id"), "service_plugin", ["service_id"], unique=False)
    op.create_index(op.f("ix_service_plugin_plugin_id"), "service_plugin", ["plugin_id"], unique=False)

    op.create_table(
        "service_plugin_version",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("service_plugin_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.Integer(), nullable=False),
        sa.Column("plugin_version_id", sa.Integer(), nullable=False),
        sa.Column("version_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_rolled_back", sa.Boolean(), nullable=False),
        # 单活:nullable unique 普通列(active 时 = f"{service_id}-{plugin_id}",否则 NULL)。
        # 评审 C3:int-int 形态最长 ~21 字符,收窄 String(191) 使 utf8mb4 UNIQUE 键长远离 InnoDB 上限。
        sa.Column("spv_active_key", sa.String(length=191), nullable=True),
        sa.Column("publish_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spv_active_key"),
    )
    op.create_index(
        op.f("ix_service_plugin_version_service_plugin_id"),
        "service_plugin_version",
        ["service_plugin_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_service_plugin_version_service_id"),
        "service_plugin_version",
        ["service_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_service_plugin_version_plugin_id"),
        "service_plugin_version",
        ["plugin_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_service_plugin_version_plugin_version_id"),
        "service_plugin_version",
        ["plugin_version_id"],
        unique=False,
    )

    op.create_table(
        "fetch_record",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("namespace_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.Integer(), nullable=False),
        sa.Column("plugin_version_id", sa.Integer(), nullable=False),
        sa.Column("fetch_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_fetch_record_namespace_id"), "fetch_record", ["namespace_id"], unique=False)
    op.create_index(op.f("ix_fetch_record_service_id"), "fetch_record", ["service_id"], unique=False)
    op.create_index(op.f("ix_fetch_record_plugin_id"), "fetch_record", ["plugin_id"], unique=False)
    op.create_index(
        op.f("ix_fetch_record_plugin_version_id"),
        "fetch_record",
        ["plugin_version_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_fetch_record_plugin_version_id"), table_name="fetch_record")
    op.drop_index(op.f("ix_fetch_record_plugin_id"), table_name="fetch_record")
    op.drop_index(op.f("ix_fetch_record_service_id"), table_name="fetch_record")
    op.drop_index(op.f("ix_fetch_record_namespace_id"), table_name="fetch_record")
    op.drop_table("fetch_record")

    op.drop_index(op.f("ix_service_plugin_version_plugin_version_id"), table_name="service_plugin_version")
    op.drop_index(op.f("ix_service_plugin_version_plugin_id"), table_name="service_plugin_version")
    op.drop_index(op.f("ix_service_plugin_version_service_id"), table_name="service_plugin_version")
    op.drop_index(op.f("ix_service_plugin_version_service_plugin_id"), table_name="service_plugin_version")
    op.drop_table("service_plugin_version")

    op.drop_index(op.f("ix_service_plugin_plugin_id"), table_name="service_plugin")
    op.drop_index(op.f("ix_service_plugin_service_id"), table_name="service_plugin")
    op.drop_table("service_plugin")

    # plugin_attachment 的 plugin_version_id 唯一约束(uq_pa_plugin_version)随 drop_table 一并移除。
    op.drop_table("plugin_attachment")

    op.drop_index(op.f("ix_plugin_version_plugin_id"), table_name="plugin_version")
    op.drop_table("plugin_version")

    op.drop_index(op.f("ix_plugin_code"), table_name="plugin")
    op.drop_table("plugin")

    op.drop_index(op.f("ix_service_namespace_id"), table_name="service")
    op.drop_table("service")

    op.drop_index(op.f("ix_namespace_code"), table_name="namespace")
    op.drop_table("namespace")
