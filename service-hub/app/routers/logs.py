from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Path, status
from fastapi.responses import StreamingResponse

from app.models import AgentLogsStreamRequest


router = APIRouter(tags=["日志流"])


def _encode_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post(
    "/api/agents/{agent_id}/logs/stream",
    summary="流式查看服务日志",
    description="通过指定 Agent 打开 docker compose logs -f --tail N 的实时日志流。",
)
async def stream_agent_logs(
    request: AgentLogsStreamRequest,
    agent_id: str = Path(title="Agent 标识", description="要查看日志的 Agent 唯一标识。"),
    requested_by: str | None = Header(default=None, alias="X-Requested-By", title="请求发起方", description="调用该接口的系统或用户标识。"),
    request_source: str | None = Header(default=None, alias="X-Requested-Source", title="请求来源", description="调用来源，例如控制台、调度器。"),
) -> StreamingResponse:
    import app.main as main_module

    agent = await main_module.hub_state.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not agent["online"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is offline")

    websocket = await main_module.hub_state.get_connection(agent_id)
    if websocket is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent connection is unavailable")

    session_id, subscriber_id, queue, start_payload = await main_module.hub_state.subscribe_log_stream(
        agent_id=agent_id,
        project_dir=request.dir,
        service=request.service,
        tail=request.tail,
        timestamps=request.timestamps,
        requested_by=requested_by,
        request_source=request_source,
    )

    if start_payload is not None:
        try:
            await websocket.send_json(start_payload)
        except Exception as exc:
            await main_module.hub_state.cancel_log_subscription(subscriber_id)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to start log stream") from exc

    async def event_stream():
        completed = False
        try:
            while True:
                event = await queue.get()
                event_name = event["event"]
                body = {
                    "sessionId": session_id,
                    "agentId": agent_id,
                }
                body.update({key: value for key, value in event.items() if key != "event"})
                yield _encode_sse(event_name, body)
                if event_name in {"finished", "error"}:
                    completed = True
                    break
        except asyncio.CancelledError:
            raise
        finally:
            stop_request = await main_module.hub_state.unsubscribe_log_stream(subscriber_id)
            if completed or stop_request is None:
                return

            active_connection = await main_module.hub_state.get_connection(stop_request["agent_id"])
            if active_connection is None:
                return

            try:
                await active_connection.send_json({"type": "logs_stop", "sessionId": stop_request["session_id"]})
            except Exception:
                main_module.logger.warning(
                    "Failed to stop log session %s for agent %s during stream cleanup",
                    stop_request["session_id"],
                    stop_request["agent_id"],
                )

    headers = {
        "Cache-Control": "no-cache",
        "X-Log-Session-Id": session_id,
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
