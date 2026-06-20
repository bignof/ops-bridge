"""add command mode column

Revision ID: 20260620_0005
Revises: 20260618_0004
Create Date: 2026-06-20 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260620_0005"
down_revision = "20260618_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("commands", sa.Column("mode", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("commands", "mode")
