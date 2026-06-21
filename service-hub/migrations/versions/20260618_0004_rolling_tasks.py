"""add rolling_tasks table

Revision ID: 20260618_0004
Revises: 20260306_0003
Create Date: 2026-06-18 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260618_0004"
down_revision = "20260306_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rolling_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("service_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active_key", sa.String(length=512), nullable=True),
        sa.Column("nodes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_rolling_tasks_task_id"), "rolling_tasks", ["task_id"], unique=True)
    op.create_index(op.f("ix_rolling_tasks_agent_id"), "rolling_tasks", ["agent_id"])
    op.create_index(op.f("ix_rolling_tasks_service_name"), "rolling_tasks", ["service_name"])
    op.create_index(op.f("ix_rolling_tasks_status"), "rolling_tasks", ["status"])
    op.create_index(op.f("ix_rolling_tasks_active_key"), "rolling_tasks", ["active_key"], unique=True)


def downgrade() -> None:
    op.drop_table("rolling_tasks")
