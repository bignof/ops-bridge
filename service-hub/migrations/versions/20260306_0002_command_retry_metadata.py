"""add command retry metadata

Revision ID: 20260306_0002
Revises: 20260306_0001
Create Date: 2026-03-06 00:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260306_0002"
down_revision = "20260306_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("commands", sa.Column("original_request_id", sa.String(length=255), nullable=True))
    op.add_column("commands", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index(op.f("ix_commands_original_request_id"), "commands", ["original_request_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_commands_original_request_id"), table_name="commands")
    op.drop_column("commands", "retry_count")
    op.drop_column("commands", "original_request_id")