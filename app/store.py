import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import WebSocket


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AgentRecord:
    agent_id: str
    connected: bool = False
    remote: str | None = None
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_pong_at: datetime | None = None


@dataclass
class CommandRecord:
    request_id: str
    agent_id: str
    payload: dict[str, Any]
    status: str
    action: str
    dir: str
    image: str | None = None
    output: str | None = None
    message: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    ack_at: datetime | None = None
    result_at: datetime | None = None


class HubState:
    def __init__(self, heartbeat_timeout: int, command_history_limit: int) -> None:
        self.heartbeat_timeout = heartbeat_timeout
        self.command_history_limit = command_history_limit
        self._agents: dict[str, AgentRecord] = {}
        self._connections: dict[str, WebSocket] = {}
        self._commands: dict[str, CommandRecord] = {}
        self._commands_by_agent: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    async def register_agent(self, agent_id: str, websocket: WebSocket, remote: str | None) -> None:
        now = utc_now()
        async with self._lock:
            record = self._agents.get(agent_id) or AgentRecord(agent_id=agent_id)
            record.connected = True
            record.remote = remote
            record.connected_at = now
            record.disconnected_at = None
            record.last_seen_at = now
            self._agents[agent_id] = record
            self._connections[agent_id] = websocket

    async def disconnect_agent(self, agent_id: str, websocket: WebSocket | None = None) -> None:
        now = utc_now()
        async with self._lock:
            active = self._connections.get(agent_id)
            if websocket is not None and active is not websocket:
                return

            self._connections.pop(agent_id, None)
            record = self._agents.get(agent_id)
            if record is not None:
                record.connected = False
                record.disconnected_at = now
                record.last_seen_at = now

    async def touch_agent(self, agent_id: str, event_type: str) -> None:
        now = utc_now()
        async with self._lock:
            record = self._agents.get(agent_id) or AgentRecord(agent_id=agent_id)
            record.last_seen_at = now
            if event_type == "heartbeat":
                record.last_heartbeat_at = now
            elif event_type == "pong":
                record.last_pong_at = now
            self._agents[agent_id] = record

    async def get_connection(self, agent_id: str) -> WebSocket | None:
        async with self._lock:
            return self._connections.get(agent_id)

    async def store_command(self, agent_id: str, payload: dict[str, Any]) -> CommandRecord:
        now = utc_now()
        request_id = payload["requestId"]
        record = CommandRecord(
            request_id=request_id,
            agent_id=agent_id,
            payload=payload,
            status="queued",
            action=payload["action"],
            dir=payload["dir"],
            image=payload.get("image"),
            created_at=now,
            updated_at=now,
        )

        async with self._lock:
            self._commands[request_id] = record
            agent_commands = self._commands_by_agent.setdefault(agent_id, [])
            agent_commands.append(request_id)
            if len(agent_commands) > self.command_history_limit:
                overflow = agent_commands[:-self.command_history_limit]
                del agent_commands[:-self.command_history_limit]
                for old_request_id in overflow:
                    self._commands.pop(old_request_id, None)
        return record

    async def mark_ack(self, request_id: str) -> CommandRecord | None:
        async with self._lock:
            record = self._commands.get(request_id)
            if record is None:
                return None
            now = utc_now()
            record.status = "processing"
            record.ack_at = now
            record.updated_at = now
            return record

    async def mark_result(
        self,
        request_id: str,
        status: str,
        *,
        output: str | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> CommandRecord | None:
        async with self._lock:
            record = self._commands.get(request_id)
            if record is None:
                return None
            now = utc_now()
            record.status = status
            record.output = output
            record.message = message
            record.error = error
            record.result_at = now
            record.updated_at = now
            return record

    async def get_command(self, request_id: str) -> CommandRecord | None:
        async with self._lock:
            return self._commands.get(request_id)

    async def list_commands(self, agent_id: str | None = None) -> list[CommandRecord]:
        async with self._lock:
            if agent_id is None:
                records = list(self._commands.values())
            else:
                request_ids = self._commands_by_agent.get(agent_id, [])
                records = [self._commands[request_id] for request_id in request_ids if request_id in self._commands]
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    async def list_agents(self) -> list[dict[str, Any]]:
        async with self._lock:
            agents = [self._snapshot_agent(record) for record in self._agents.values()]
        return sorted(agents, key=lambda item: item["agent_id"])

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        async with self._lock:
            record = self._agents.get(agent_id)
            if record is None:
                return None
            return self._snapshot_agent(record)

    def _snapshot_agent(self, record: AgentRecord) -> dict[str, Any]:
        last_seen_at = record.last_seen_at
        online = bool(
            record.connected
            and last_seen_at is not None
            and utc_now() - last_seen_at <= timedelta(seconds=self.heartbeat_timeout)
        )
        return {
            "agent_id": record.agent_id,
            "connected": record.connected,
            "online": online,
            "remote": record.remote,
            "connected_at": record.connected_at,
            "disconnected_at": record.disconnected_at,
            "last_seen_at": record.last_seen_at,
            "last_heartbeat_at": record.last_heartbeat_at,
            "last_pong_at": record.last_pong_at,
            "stale_after_seconds": self.heartbeat_timeout,
        }


def command_to_dict(record: CommandRecord) -> dict[str, Any]:
    return asdict(record)
