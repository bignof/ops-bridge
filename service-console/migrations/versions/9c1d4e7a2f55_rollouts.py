"""rollouts

Revision ID: 9c1d4e7a2f55
Revises: 3bff85827d8c
Create Date: 2026-06-23 00:00:00.000000

投放运行记录(P4-2/P4-3)落地表。一次「显式投放(Rollout)」= 把 desired-state 推到运行实例
的一轮编排;status 与底层 rolling_task 终态对齐回写,frozen 标记失败即停留下的半迁移态等人工。
并发互斥复用 rolling_tasks.active_key,本表不另立锁(故无唯一约束,仅 service_name 索引便于按服务查)。
"""
from alembic import op
import sqlalchemy as sa


revision = '9c1d4e7a2f55'
down_revision = '3bff85827d8c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('rollouts',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('namespace', sa.String(length=255), nullable=True),
    sa.Column('service_name', sa.String(length=255), nullable=False),
    sa.Column('mode', sa.String(length=32), nullable=False),
    sa.Column('trigger', sa.String(length=32), nullable=False),
    sa.Column('target', sa.Text(), nullable=True),
    sa.Column('previous_target', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('frozen', sa.Boolean(), nullable=False),
    sa.Column('rolling_task_id', sa.String(length=64), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('force', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_rollouts_service_name'), 'rollouts', ['service_name'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_rollouts_service_name'), table_name='rollouts')
    op.drop_table('rollouts')
