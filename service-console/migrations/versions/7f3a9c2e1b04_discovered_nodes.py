"""discovered_nodes

Revision ID: 7f3a9c2e1b04
Revises: 682a89c2f7d1
Create Date: 2026-06-22 00:00:00.000000

agent 周期发现上报(P3-3/P3-4)落地表。一行 = 某 agent 名下的一个容器节点;
(agent_id, container_name) 唯一。失联/本轮缺席的行只标 status=stale 不删(评审 M8)。
"""
from alembic import op
import sqlalchemy as sa


revision = '7f3a9c2e1b04'
down_revision = '682a89c2f7d1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('discovered_nodes',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('agent_id', sa.String(length=255), nullable=False),
    sa.Column('container_name', sa.String(length=255), nullable=False),
    sa.Column('container_id', sa.String(length=255), nullable=True),
    sa.Column('compose_project', sa.String(length=255), nullable=True),
    sa.Column('compose_service', sa.String(length=255), nullable=True),
    sa.Column('dir', sa.String(length=2048), nullable=True),
    sa.Column('image', sa.String(length=2048), nullable=True),
    sa.Column('running', sa.Boolean(), nullable=False),
    sa.Column('nacos_service', sa.String(length=255), nullable=True),
    sa.Column('healthy', sa.Boolean(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agent_id', 'container_name', name='uq_dn_agent_container')
    )
    op.create_index(op.f('ix_discovered_nodes_agent_id'), 'discovered_nodes', ['agent_id'], unique=False)
    op.create_index(op.f('ix_discovered_nodes_compose_project'), 'discovered_nodes', ['compose_project'], unique=False)
    op.create_index(op.f('ix_discovered_nodes_nacos_service'), 'discovered_nodes', ['nacos_service'], unique=False)
    op.create_index(op.f('ix_discovered_nodes_status'), 'discovered_nodes', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_discovered_nodes_status'), table_name='discovered_nodes')
    op.drop_index(op.f('ix_discovered_nodes_nacos_service'), table_name='discovered_nodes')
    op.drop_index(op.f('ix_discovered_nodes_compose_project'), table_name='discovered_nodes')
    op.drop_index(op.f('ix_discovered_nodes_agent_id'), table_name='discovered_nodes')
    op.drop_table('discovered_nodes')
