import json
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status

from app.config import settings
from app.models import AgentSnapshot, CommandDispatchRequest, CommandDispatchResponse, CommandSnapshot
from app.store import HubState, command_to_dict


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="service-hub", version="0.1.0")
hub_state = HubState(
    heartbeat_timeout=settings.heartbeat_timeout,
    command_history_limit=settings.command_history_limit,
)


def _remote_address(websocket: WebSocket) -> str | None:
    if websocket.client is None:
        return None
    return f"{websocket.client.host}:{websocket.client.port}"


async def _serialize_command(request_id: str) -> CommandSnapshot:
    record = await hub_state.get_command(request_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")
    return CommandSnapshot.model_validate(command_to_dict(record))


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


@app.get("/api/agents/{agent_id}/commands", response_model=list[CommandSnapshot])
async def list_agent_commands(agent_id: str) -> list[CommandSnapshot]:
    commands = await hub_state.list_commands(agent_id=agent_id)
    return [CommandSnapshot.model_validate(command_to_dict(item)) for item in commands]


@app.get("/api/commands/{request_id}", response_model=CommandSnapshot)
async def get_command(request_id: str) -> CommandSnapshot:
    return await _serialize_command(request_id)


@app.post("/api/agents/{agent_id}/commands", response_model=CommandDispatchResponse, status_code=status.HTTP_202_ACCEPTED)
async def dispatch_command(agent_id: str, request: CommandDispatchRequest) -> CommandDispatchResponse:
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

    await hub_state.store_command(agent_id, payload)

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


@app.websocket("/ws/agent/{agent_id}")
async def agent_ws(websocket: WebSocket, agent_id: str) -> None:
    token = websocket.query_params.get("token", "")
    if token != settings.auth_token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("Rejected agent %s due to invalid token", agent_id)
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
