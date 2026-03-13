"""add per-agent credentials

Revision ID: 20260306_0003
Revises: 20260306_0002
Create Date: 2026-03-06 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260306_0003"
down_revision = "20260306_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("agent_key_hash", sa.String(length=64), nullable=True))
    op.add_column("agents", sa.Column("key_issued_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "key_issued_at")
    op.drop_column("agents", "agent_key_hash")