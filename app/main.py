import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Header, Query, WebSocket, WebSocketDisconnect, status

from app.config import settings
from app.db import Database
from app.models import AgentCredentialResponse, AgentProvisionRequest, AgentProvisionResponse, AgentSnapshot, CommandDispatchRequest, CommandDispatchResponse, CommandEventSnapshot, CommandListResponse, CommandSnapshot
from app.store import HubState


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await hub_state.initialize()
    yield

app = FastAPI(title="service-hub", version="0.1.0", lifespan=lifespan)
database = Database(settings.database_url)
hub_state = HubState(
    heartbeat_timeout=settings.heartbeat_timeout,
    command_history_limit=settings.command_history_limit,
    database=database,
)


def _remote_address(websocket: WebSocket) -> str | None:
    if websocket.client is None:
        return None
    return f"{websocket.client.host}:{websocket.client.port}"


def _require_admin_token(admin_token: str | None) -> None:
    if admin_token != settings.admin_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid admin token")


async def _build_command_list_response(
    *,
    agent_id: str | None,
    status_filter: str | None,
    action: str | None,
    requested_by: str | None,
    request_source: str | None,
    created_after: datetime | None,
    created_before: datetime | None,
    sort_by: str,
    order: str,
    limit: int,
    offset: int,
) -> CommandListResponse:
    result = await hub_state.list_commands(
        agent_id=agent_id,
        status=status_filter,
        action=action,
        requested_by=requested_by,
        request_source=request_source,
        created_after=created_after,
        created_before=created_before,
        sort_by=sort_by,
        order=order,
        limit=limit,
        offset=offset,
    )
    return CommandListResponse(
        items=[CommandSnapshot.model_validate(item) for item in result["items"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
        has_more=result["has_more"],
        sort_by=result["sort_by"],
        order=result["order"],
    )


async def _serialize_command(request_id: str) -> CommandSnapshot:
    record = await hub_state.get_command(request_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")
    return CommandSnapshot.model_validate(record)


async def _handle_agent_message(agent_id: str, payload: dict[str, Any]) -> None:
    msg_type = payload.get("type")
    if not msg_type:
        logger.warning("Agent %s sent message without type: %s", agent_id, payload)
        return

    await hub_state.touch_agent(agent_id, msg_type)

    if msg_type == "heartbeat":
        return

    if msg_type == "ack":
        request_id = payload.get("requestId")
        if request_id:
            await hub_state.mark_ack(request_id)
        return

    if msg_type == "result":
        request_id = payload.get("requestId")
        if request_id:
            await hub_state.mark_result(
                request_id,
                payload.get("status", "failed"),
                output=payload.get("output"),
                message=payload.get("message"),
                error=payload.get("error"),
            )
        return

    if msg_type == "pong":
        return

    logger.info("Unhandled message type from %s: %s", agent_id, msg_type)


@app.get("/health")
async def health() -> dict[str, str]:
    await hub_state.check_database()
    return {"status": "ok"}


@app.get("/api/agents", response_model=list[AgentSnapshot])
async def list_agents() -> list[AgentSnapshot]:
    agents = await hub_state.list_agents()
    return [AgentSnapshot.model_validate(agent) for agent in agents]


@app.get("/api/agents/{agent_id}", response_model=AgentSnapshot)
async def get_agent(agent_id: str) -> AgentSnapshot:
    agent = await hub_state.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return AgentSnapshot.model_validate(agent)


@app.post("/api/agents", response_model=AgentProvisionResponse, status_code=status.HTTP_201_CREATED)
async def provision_agent(
    request: AgentProvisionRequest,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> AgentProvisionResponse:
    _require_admin_token(admin_token)
    provisioned = await hub_state.provision_agent(request.agent_id)
    if provisioned is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent already exists")

    return AgentProvisionResponse(
        agent=AgentSnapshot.model_validate(provisioned["agent"]),
        agent_key=provisioned["agent_key"],
        issued_at=provisioned["issued_at"],
    )


@app.post("/api/agents/{agent_id}/credentials/rotate", response_model=AgentCredentialResponse)
async def rotate_agent_credentials(
    agent_id: str,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> AgentCredentialResponse:
    _require_admin_token(admin_token)
    credential = await hub_state.rotate_agent_key(agent_id)
    return AgentCredentialResponse.model_validate(credential)


@app.get("/api/agents/{agent_id}/commands", response_model=CommandListResponse)
async def list_agent_commands(
    agent_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    action: str | None = Query(default=None),
    requested_by: str | None = Query(default=None, alias="requestedBy"),
    request_source: str | None = Query(default=None, alias="requestSource"),
    created_after: datetime | None = Query(default=None, alias="createdAfter"),
    created_before: datetime | None = Query(default=None, alias="createdBefore"),
    sort_by: str = Query(default="createdAt", alias="sortBy", pattern="^(createdAt|updatedAt)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> CommandListResponse:
    return await _build_command_list_response(
        agent_id=agent_id,
        status_filter=status_filter,
        action=action,
        requested_by=requested_by,
        request_source=request_source,
        created_after=created_after,
        created_before=created_before,
        sort_by=sort_by,
        order=order,
        limit=limit,
        offset=offset,
    )


@app.get("/api/commands", response_model=CommandListResponse)
async def list_commands(
    agent_id: str | None = Query(default=None, alias="agentId"),
    status_filter: str | None = Query(default=None, alias="status"),
    action: str | None = Query(default=None),
    requested_by: str | None = Query(default=None, alias="requestedBy"),
    request_source: str | None = Query(default=None, alias="requestSource"),
    created_after: datetime | None = Query(default=None, alias="createdAfter"),
    created_before: datetime | None = Query(default=None, alias="createdBefore"),
    sort_by: str = Query(default="createdAt", alias="sortBy", pattern="^(createdAt|updatedAt)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> CommandListResponse:
    return await _build_command_list_response(
        agent_id=agent_id,
        status_filter=status_filter,
        action=action,
        requested_by=requested_by,
        request_source=request_source,
        created_after=created_after,
        created_before=created_before,
        sort_by=sort_by,
        order=order,
        limit=limit,
        offset=offset,
    )


@app.get("/api/commands/{request_id}", response_model=CommandSnapshot)
async def get_command(request_id: str) -> CommandSnapshot:
    return await _serialize_command(request_id)


@app.get("/api/commands/{request_id}/events", response_model=list[CommandEventSnapshot])
async def get_command_events(request_id: str) -> list[CommandEventSnapshot]:
    command = await hub_state.get_command(request_id)
    if command is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")

    events = await hub_state.list_command_events(request_id)
    return [CommandEventSnapshot.model_validate(item) for item in events]


@app.post("/api/agents/{agent_id}/commands", response_model=CommandDispatchResponse, status_code=status.HTTP_202_ACCEPTED)
async def dispatch_command(
    agent_id: str,
    request: CommandDispatchRequest,
    requested_by: str | None = Header(default=None, alias="X-Requested-By"),
    request_source: str | None = Header(default=None, alias="X-Requested-Source"),
) -> CommandDispatchResponse:
    agent = await hub_state.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not agent["online"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is offline")

    payload = {
        "type": "command",
        "requestId": request.request_id,
        "action": request.action,
        "dir": request.dir,
    }
    if request.image:
        payload["image"] = request.image

    await hub_state.store_command(
        agent_id,
        payload,
        requested_by=requested_by,
        request_source=request_source,
    )

    websocket = await hub_state.get_connection(agent_id)
    if websocket is None:
        await hub_state.mark_result(request.request_id, "failed", error="Agent connection is unavailable")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent connection is unavailable")

    try:
        await websocket.send_json(payload)
        logger.info("Dispatched command %s to agent %s", request.request_id, agent_id)
    except Exception as exc:
        logger.exception("Failed to dispatch command %s to agent %s", request.request_id, agent_id)
        await hub_state.mark_result(request.request_id, "failed", error=f"Failed to dispatch command: {exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to dispatch command") from exc

    command = await _serialize_command(request.request_id)
    return CommandDispatchResponse(accepted=True, command=command)


@app.post("/api/commands/{request_id}/retry", response_model=CommandDispatchResponse, status_code=status.HTTP_202_ACCEPTED)
async def retry_command(
    request_id: str,
    requested_by: str | None = Header(default=None, alias="X-Requested-By"),
    request_source: str | None = Header(default=None, alias="X-Requested-Source"),
) -> CommandDispatchResponse:
    original = await hub_state.get_command(request_id)
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")
    if original["status"] != "failed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only failed commands can be retried")

    agent = await hub_state.get_agent(original["agent_id"])
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not agent["online"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is offline")

    retried = await hub_state.retry_command(
        request_id,
        requested_by=requested_by,
        request_source=request_source,
    )
    if retried is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")

    _, retry_record = retried
    websocket = await hub_state.get_connection(retry_record["agent_id"])
    if websocket is None:
        await hub_state.mark_result(retry_record["request_id"], "failed", error="Agent connection is unavailable")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent connection is unavailable")

    try:
        await websocket.send_json(retry_record["payload"])
        logger.info("Retried command %s as %s for agent %s", request_id, retry_record["request_id"], retry_record["agent_id"])
    except Exception as exc:
        logger.exception("Failed to retry command %s for agent %s", request_id, retry_record["agent_id"])
        await hub_state.mark_result(retry_record["request_id"], "failed", error=f"Failed to dispatch command: {exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to dispatch command") from exc

    return CommandDispatchResponse(accepted=True, command=await _serialize_command(retry_record["request_id"]))


@app.websocket("/ws/agent/{agent_id}")
async def agent_ws(websocket: WebSocket, agent_id: str) -> None:
    presented_key = websocket.query_params.get("key", "")
    if not await hub_state.authenticate_agent(agent_id, presented_key):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("Rejected agent %s due to invalid credentials", agent_id)
        return

    await websocket.accept()
    await hub_state.register_agent(agent_id, websocket, _remote_address(websocket))
    logger.info("Agent %s connected", agent_id)

    try:
        while True:
            message = await websocket.receive()
            if "text" in message and message["text"] is not None:
                payload = json.loads(message["text"])
                if isinstance(payload, dict):
                    await _handle_agent_message(agent_id, payload)
                else:
                    logger.warning("Agent %s sent non-object payload", agent_id)
            elif message.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        logger.info("Agent %s disconnected", agent_id)
    except json.JSONDecodeError:
        logger.warning("Agent %s sent invalid JSON", agent_id)
    except Exception:
        logger.exception("Agent %s websocket loop failed", agent_id)
    finally:
        await hub_state.disconnect_agent(agent_id, websocket)
