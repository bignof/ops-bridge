from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AgentModel(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="offline")
    remote_addr: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
