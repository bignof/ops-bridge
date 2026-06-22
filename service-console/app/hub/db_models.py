from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AgentModel(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="offline")
    agent_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remote_addr: Mapped[str | None] = mapped_column(String(255), nullable=True)
    key_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_pong_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_disconnect_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CommandModel(Base):
    __tablename__ = "commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    action: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_dir: Mapped[str] = mapped_column(String(2048))
    target_image: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    original_request_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommandEventModel(Base):
    __tablename__ = "command_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(255), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RollingTaskModel(Base):
    __tablename__ = "rolling_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    service_name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)        # running/done/failed/interrupted
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    active_key: Mapped[str | None] = mapped_column(String(512), nullable=True, unique=True)
    nodes_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DiscoveredNodeModel(Base):
    # agent 周期发现上报(P3-3/P3-4)落地表:一行 = 某 agent 名下的一个容器节点。
    # 行承载 dir/工程定位信息,失联/缺席只标 status=stale 不删(评审 M8),仅显式下线才删(本任务不做删)。
    __tablename__ = "discovered_nodes"
    __table_args__ = (UniqueConstraint("agent_id", "container_name", name="uq_dn_agent_container"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    container_name: Mapped[str] = mapped_column(String(255))
    container_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    compose_project: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    compose_service: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dir: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    image: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    nacos_service: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    healthy: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # active|stale:本轮上报命中=active,该 agent 名下本轮缺席的行=stale(不删行)。
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
