import asyncio
import logging
import uuid

from fastapi import APIRouter, Header, HTTPException, Path, status
from pydantic import BaseModel

from app.api_support import _require_admin_token
from app.store import RollingConflict

logger = logging.getLogger(__name__)

router = APIRouter(tags=["滚动重启"])

_background: set[asyncio.Task] = set()


def _on_task_done(task: asyncio.Task) -> None:
    # 后台滚动任务收尾:既从 _background 移除,也读取并记录异常,
    # 避免 agent 断连等失败"零日志"(_run_rolling 自身已兜底落库+释放锁,
    # 此处兜的是 _run_rolling 之外/finish 落库失败逃逸出来的异常)。
    _background.discard(task)
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.error("rolling 后台任务异常", exc_info=exc)


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
        # 真空集(nacos 未发现实例或全部不健康)即便 force 也不算成功(spec §10 noInstances)
        if not healthy:
            await hub_state.finish_rolling(task_id, "failed",
                error="无健康实例可滚动(nacos 未发现实例;检查 serviceName/namespace 是否正确)")
            return
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
                # 失败即停:把尚未处理的剩余节点标 skipped,便于运维区分"未动过"与"被中止跳过"
                for n in nodes[idx + 1:]:
                    n["status"] = "skipped"
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
    task.add_done_callback(_on_task_done)
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


@router.post("/api/rolling-restart/{task_id}/acknowledge")
async def acknowledge_rolling(
    task_id: str = Path(...),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
):
    # 人工确认 hub 重启遗留的 interrupted 任务:释放 active_key,放行同 key 的新滚动。
    _require_admin_token(admin_token)
    import app.main as main_module
    task = await main_module.hub_state.get_rolling_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task 不存在")
    if task["status"] != "interrupted":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"task 当前状态为 {task['status']},仅 interrupted 任务可确认")
    await main_module.hub_state.acknowledge_rolling(task_id)
    return {"acknowledged": True}
