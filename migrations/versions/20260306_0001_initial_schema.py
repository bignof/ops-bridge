"""initial schema

Revision ID: 20260306_0001
Revises:
Create Date: 2026-03-06 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260306_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("remote_addr", sa.String(length=255), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_pong_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_disconnect_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id"),
    )
    op.create_index(op.f("ix_agents_agent_id"), "agents", ["agent_id"], unique=True)

    op.create_table(
        "commands",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=255), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_dir", sa.String(length=2048), nullable=False),
        sa.Column("target_image", sa.String(length=2048), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=True),
        sa.Column("request_source", sa.String(length=255), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ack_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index(op.f("ix_commands_request_id"), "commands", ["request_id"], unique=True)
    op.create_index(op.f("ix_commands_agent_id"), "commands", ["agent_id"], unique=False)
    op.create_index(op.f("ix_commands_status"), "commands", ["status"], unique=False)

    op.create_table(
        "command_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_command_events_request_id"), "command_events", ["request_id"], unique=False)
    op.create_index(op.f("ix_command_events_event_type"), "command_events", ["event_type"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_command_events_event_type"), table_name="command_events")
    op.drop_index(op.f("ix_command_events_request_id"), table_name="command_events")
    op.drop_table("command_events")

    op.drop_index(op.f("ix_commands_status"), table_name="commands")
    op.drop_index(op.f("ix_commands_agent_id"), table_name="commands")
    op.drop_index(op.f("ix_commands_request_id"), table_name="commands")
    op.drop_table("commands")

    op.drop_index(op.f("ix_agents_agent_id"), table_name="agents")
    op.drop_table("agents")