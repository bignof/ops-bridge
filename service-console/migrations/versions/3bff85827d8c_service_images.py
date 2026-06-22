"""service_images

Revision ID: 3bff85827d8c
Revises: 7f3a9c2e1b04
Create Date: 2026-06-22 00:00:00.000000

镜像台账(P4-4)落地表。一行 = 某 service 曾用/在用的一个镜像;同 service 多行(历史),
至多一行 is_current=True(单活由 app 在 set_current_image 事务内维护,不设 DB 兜底)。
本期纯台账,不接 redeploy 寻址(后续 P4-2/P4-5)。
"""
from alembic import op
import sqlalchemy as sa


revision = '3bff85827d8c'
down_revision = '7f3a9c2e1b04'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('service_images',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('service_id', sa.Integer(), nullable=False),
    sa.Column('image', sa.String(length=2048), nullable=False),
    sa.Column('is_current', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_service_images_service_id'), 'service_images', ['service_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_service_images_service_id'), table_name='service_images')
    op.drop_table('service_images')
