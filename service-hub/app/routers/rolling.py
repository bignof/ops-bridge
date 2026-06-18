import asyncio
import uuid

from fastapi import APIRouter, Header, HTTPException, Path, status
from pydantic import BaseModel

from app.api_support import _require_admin_token
from app.store import RollingConflict

router = APIRouter(tags=["滚动重启"])

_background: set[asyncio.Task] = set()


class RollingRestartRequest(BaseModel):
    agentId: str
    serviceName: str
    force: bool = False


async def _run_rolling(task_id, agent_id, service_name, force, hub_state, settings):
    try:
        req = str(uuid.uuid4())
        listed = await hub_state.call_agent(
            agent_id, {"type": "list-instances", "requestId": req, "serviceName": service_name},
            timeout=settings.rolling_cmd_timeout)
        if listed.get("status") != "success":
            await hub_state.finish_rolling(task_id, "failed", error=f"list-instances 失败: {listed.get('error')}")
            return
        instances = listed.get("instances") or []
        unmatched = [i["address"] for i in instances if not i.get("matched")]
        if unmatched:
            await hub_state.finish_rolling(task_id, "failed", error=f"实例对不上号(可能非本机或匹配键错): {unmatched}")
            return
        healthy = [i for i in instances if i.get("healthy")]
        if len(healthy) < 2 and not force:
            await hub_state.finish_rolling(task_id, "failed",
                error=f"健康实例数={len(healthy)}<2,无法零中断滚动;请扩容或 force")
            return
        nodes = [{"address": i["address"], "containerId": i["containerId"], "status": "pending"} for i in healthy]
        await hub_state.update_rolling_nodes(task_id, nodes)
        for idx, node in enumerate(nodes):
            nodes[idx]["status"] = "in-progress"
            await hub_state.update_rolling_nodes(task_id, nodes)
            req = str(uuid.uuid4())
            res = await hub_state.call_agent(agent_id, {
                "type": "graceful-restart", "requestId": req,
                "containerId": node["containerId"],
                "healthBaseUrl": f"http://{node['address']}",
                "settleSec": settings.rolling_settle_sec,
                "shutdownTimeoutSec": settings.rolling_shutdown_timeout,
                "readyTimeoutSec": settings.rolling_ready_timeout,
            }, timeout=settings.rolling_cmd_timeout)
            if res.get("status") != "success":
                nodes[idx]["status"] = "failed"
                nodes[idx]["error"] = res.get("error")
                await hub_state.finish_rolling(task_id, "failed", nodes=nodes,
                    error=f"节点 {node['address']} 失败,停止滚动")
                return
            nodes[idx]["status"] = "done"
            await hub_state.update_rolling_nodes(task_id, nodes)
        await hub_state.finish_rolling(task_id, "degraded" if (len(healthy) < 2) else "done",
                                       nodes=nodes, degraded=(len(healthy) < 2))
    except asyncio.TimeoutError:
        await hub_state.finish_rolling(task_id, "failed", error="等待 agent 命令结果超时")
    except Exception as exc:  # noqa: BLE001
        await hub_state.finish_rolling(task_id, "failed", error=str(exc))


@router.post("/api/rolling-restart")
async def rolling_restart(
    request: RollingRestartRequest,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
):
    _require_admin_token(admin_token)
    import app.main as main_module
    task_id = str(uuid.uuid4())
    try:
        await main_module.hub_state.create_rolling_task(
            task_id, request.agentId, request.serviceName, request.force)
    except RollingConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    task = asyncio.create_task(
        _run_rolling(task_id, request.agentId, request.serviceName, request.force,
                     main_module.hub_state, main_module.settings))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"taskId": task_id}


@router.get("/api/rolling-restart/{task_id}")
async def get_rolling(
    task_id: str = Path(...),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
):
    _require_admin_token(admin_token)
    import app.main as main_module
    task = await main_module.hub_state.get_rolling_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task 不存在")
    return task
