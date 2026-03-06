from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


MODEL_CONFIG = ConfigDict(
    alias_generator=to_camel,
    populate_by_name=True,
    serialize_by_alias=True,
)


class CommandDispatchRequest(BaseModel):
    model_config = MODEL_CONFIG

    request_id: str = Field(default_factory=lambda: str(uuid4()))
    action: Literal["update", "restart"]
    dir: str
    image: str | None = None

    @model_validator(mode="after")
    def validate_image(self) -> "CommandDispatchRequest":
        if self.action == "update" and not self.image:
            raise ValueError("Action 'update' requires the 'image' field")
        return self


class AgentSnapshot(BaseModel):
    model_config = MODEL_CONFIG

    agent_id: str
    connected: bool
    online: bool
    remote: str | None = None
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_pong_at: datetime | None = None
    stale_after_seconds: int


class CommandSnapshot(BaseModel):
    model_config = MODEL_CONFIG

    request_id: str
    agent_id: str
    status: str
    action: str
    dir: str
    image: str | None = None
    payload: dict[str, Any]
    output: str | None = None
    message: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    ack_at: datetime | None = None
    result_at: datetime | None = None


class CommandDispatchResponse(BaseModel):
    model_config = MODEL_CONFIG

    accepted: bool
    command: CommandSnapshot
