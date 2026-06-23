from app.config import settings as _hub_settings
import asyncio
import logging
import uuid

from fastapi import APIRouter, Header, HTTPException, Path, status
from pydantic import BaseModel

from app.hub.api_support import _require_admin_token
from app.hub.store import RollingConflict

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
                     main_module.hub_state, _hub_settings))
    _background.add(task)
    task.add_done_callback(_on_task_done)
    return {"taskId": task_id}


# 跨 agent(跨机)滚动用的哨兵 agent_id:跨机锁键以 service_name 为单位,
# RollingTaskModel.agent_id 无单一归属,占位为 "*"(仅审计展示用,不参与锁/寻址)。
_CROSS_AGENT_SENTINEL = "*"


async def _run_service_rolling(task_id, service_name, force, hub_state, settings, rollout_id=None):
    """跨 agent(跨机)顺序滚动协调器(P4-1,设计 §4.1)。

    跨机零中断 = 把承载同一 nacos 服务的**各 agent 的 matched-local healthy 实例**汇成一条
    全局有序列表,**全局一次只滚一个**(每个 graceful drain→restart→wait-ready 完才下一个),
    从而任一时刻至多一个实例 down。与单 agent `_run_rolling` 的关键差异:① 实例来源跨多个
    agent(靠自动发现的 DiscoveredNode 按 nacos 名聚合定位 agent);② unmatched 实例不再整体
    abort,而是**过滤丢弃**(unmatched = 别机实例,本协调器逐 agent 各取本机 matched);③ 集群
    健康门 ≥2 从「单机内」升级为「集群级」(跨 agent 实例合计)。失败收敛 = freeze(失败即停,
    不回滚)。

    `rollout_id`(P4-2/P4-3,默认 None = 现状,裸滚不受影响):非空时本协调器除了写 rolling_task
    终态,还把同一终态回写到 rollouts 表 —— done/degraded 写终态不冻结;failed 置 frozen=True
    (失败即停留下半迁移态,标记「冻结待人工」)。为保证**每条终态出口**(8 个:无发现实例 / 某
    agent list 失败 / total==0 / total<2 非 force / graceful 失败即停 / 全成功 / 超时 / 通用异常)
    都同步回写、不漏分支,把 finish_rolling 收敛进下方局部 `_finish` 闭包统一落两边。rollout_id=None
    时 `_finish` 行为与改造前逐一调 hub_state.finish_rolling 完全一致(只是多一层透传)。

    TODO(后续加固,本期不做):continuous cross-probe —— 滚 A 期间持续重探 B/C 健康。MVP 靠
    「全局一次只滚一个 + 每步 wait-ready」已保证至多一个 down;对「独立第三方实例在滚动期间自行
    故障」的连续探测是后续加固项,不在本期范围。
    """
    import app.store as store

    async def _finish(status_value, *, nodes=None, error=None, degraded=False):
        # 先写 rolling_task 终态(行为同改造前);再(仅当本次投放有 rollout_id)把同一终态回写
        # rollouts 表。failed → frozen=True(半迁移态等人工);done/degraded → 不冻结。
        await hub_state.finish_rolling(task_id, status_value, nodes=nodes, error=error, degraded=degraded)
        if rollout_id is not None:
            await asyncio.to_thread(
                store.finish_rollout, rollout_id, status_value,
                error=error, frozen=(status_value == "failed"), rolling_task_id=task_id)

    try:
        # 1. 定位 agents:按 nacos 名聚合 active 发现实例 → 取承载该服务的不重复 agent(稳定序)。
        agg = await asyncio.to_thread(store.aggregate_discovered_by_nacos, "active")
        group = agg.get(service_name, [])
        agent_ids: list[str] = []
        for dn in group:
            if dn.agent_id not in agent_ids:
                agent_ids.append(dn.agent_id)
        if not agent_ids:
            await _finish("failed", error="无发现实例(nacos 未发现该服务的活跃实例)")
            return

        # 2. 逐 agent 收集 matched-local healthy 实例 → 跨 agent 有序列表(先按 agent 序、agent 内按返回序)。
        targets: list[dict] = []
        for agent_id in agent_ids:
            req = str(uuid.uuid4())
            listed = await hub_state.call_agent(
                agent_id,
                {"type": "list-instances", "requestId": req, "serviceName": service_name},
                timeout=settings.rolling_cmd_timeout)
            if listed.get("status") != "success":
                await _finish(
                    "failed",
                    error=f"list-instances 失败(agent {agent_id}): {listed.get('error')}")
                return
            for inst in listed.get("instances") or []:
                # unmatched = 别机实例,过滤丢弃(不再 abort);只滚本机 matched 且 healthy 的实例。
                if inst.get("matched") and inst.get("healthy"):
                    targets.append({
                        "agentId": agent_id,
                        "address": inst["address"],
                        "containerId": inst["containerId"],
                    })

        # 3. 集群健康门(跨 agent 实例合计):0 → 无可滚;<2 且非 force → 拒(零中断不可达)。
        total = len(targets)
        if total == 0:
            await _finish(
                "failed", error="无可滚实例(各 agent 均无 matched 且 healthy 的本机实例)")
            return
        if total < 2 and not force:
            await _finish(
                "failed", error=f"集群健康实例数={total}<2,无法零中断滚动;扩容或 force")
            return

        # 4. 全局逐一滚:先把全部 targets 落 nodes(pending,带 agentId+address+containerId),再按序滚。
        nodes = [{"agentId": t["agentId"], "address": t["address"],
                  "containerId": t["containerId"], "status": "pending"} for t in targets]
        await hub_state.update_rolling_nodes(task_id, nodes)
        for idx, node in enumerate(nodes):
            nodes[idx]["status"] = "in-progress"
            await hub_state.update_rolling_nodes(task_id, nodes)
            req = str(uuid.uuid4())
            res = await hub_state.call_agent(node["agentId"], {
                "type": "graceful-restart", "requestId": req,
                "containerId": node["containerId"],
                "healthBaseUrl": f"http://{node['address']}",
                "settleSec": settings.rolling_settle_sec,
                "shutdownTimeoutSec": settings.rolling_shutdown_timeout,
                "readyTimeoutSec": settings.rolling_ready_timeout,
            }, timeout=settings.rolling_cmd_timeout)
            if res.get("status") != "success":
                # 失败即停(freeze,不回滚):该节点 failed,余下全部标 skipped。
                nodes[idx]["status"] = "failed"
                nodes[idx]["error"] = res.get("error")
                for n in nodes[idx + 1:]:
                    n["status"] = "skipped"
                await _finish(
                    "failed", nodes=nodes,
                    error=f"节点 {node['agentId']}/{node['address']} 失败,停止滚动")
                return
            nodes[idx]["status"] = "done"
            await hub_state.update_rolling_nodes(task_id, nodes)

        # 5. 全部成功:集群实例 <2(force 放行)标 degraded,否则 done。
        await _finish(
            "degraded" if total < 2 else "done", nodes=nodes, degraded=(total < 2))
    except asyncio.TimeoutError:
        await _finish("failed", error="等待 agent 命令结果超时")
    except Exception as exc:  # noqa: BLE001
        await _finish("failed", error=str(exc))


class ServiceRollingRequest(BaseModel):
    serviceName: str
    force: bool = False


@router.post("/api/service-rolling")
async def service_rolling(
    request: ServiceRollingRequest,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
):
    # 跨 agent(跨机)顺序滚动入口:锁键以 service_name 为单位(agent_id 用哨兵 "*"),
    # 防同一 nacos 服务被并发跨机滚。建 task 后台执行,与单 agent 端点同构。
    _require_admin_token(admin_token)
    import app.main as main_module
    task_id = str(uuid.uuid4())
    try:
        await main_module.hub_state.create_rolling_task(
            task_id, _CROSS_AGENT_SENTINEL, request.serviceName, request.force,
            active_key=request.serviceName)
    except RollingConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    task = asyncio.create_task(
        _run_service_rolling(task_id, request.serviceName, request.force,
                             main_module.hub_state, _hub_settings))
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
