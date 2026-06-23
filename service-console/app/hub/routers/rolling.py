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


async def _run_service_rolling(
    task_id, service_name, force, hub_state, settings,
    rollout_id=None, instance_filter=None, mode="restart", image=None,
):
    """跨 agent(跨机)顺序滚动协调器(P4-1,设计 §4.1)。

    跨机零中断 = 把承载同一 nacos 服务的**各 agent 的 matched-local healthy 实例**汇成一条
    全局有序列表,**全局一次只滚一个**(每个 graceful drain→restart→wait-ready 完才下一个),
    从而任一时刻至多一个实例 down。与单 agent `_run_rolling` 的关键差异:① 实例来源跨多个
    agent(靠自动发现的 DiscoveredNode 按 nacos 名聚合定位 agent);② unmatched 实例不再整体
    abort,而是**过滤丢弃**(unmatched = 别机实例,本协调器逐 agent 各取本机 matched);③ 集群
    健康门 ≥2 从「单机内」升级为「集群级」(跨 agent 实例合计)。失败收敛 = freeze(失败即停,
    不回滚)。

    `mode`(零中断滚动重拉镜像,默认 'restart' = 现状):
    - `'restart'`:逐实例发 `graceful-restart`(原地重启容器)。**逐行不变**,见下「等价回归保证」。
    - `'pull-redeploy'`:逐实例改发 `graceful-redeploy`(单实例 drain→拉新镜像→重建→wait-ready),
      payload 额外带 `image`(= 入参 image,目标镜像)与 `dir`(= **该实例 containerId 对应的 compose
      工程目录**)。**dir 解析来源**:从 `group`(= agg[service_name] 的 DiscoveredNode 行,发现权威)
      构建 `{dn.container_id: dn.dir}` 映射,按 node 的 containerId 取 dir;**取不到 dir → 该实例失败
      即停**(无 dir 无法定位 compose 工程,继续滚会拉错目录)。其余(健康门 / instance_filter 灰度 /
      一次只滚一个 / 失败即停 / degraded / _finish 回写)与 restart **完全一致,不分 mode 复制一套**——
      只在「发哪个命令 + 带不带 image/dir」处分流。
    - 等待 result type 在 'pull-redeploy' 下是 `graceful-redeploy-result`,但 `call_agent` 按 requestId
      路由响应(不校验 type,见 hub.store.call_agent / resolve_pending),故 payload type 不同也能 await
      到,`res.get("status")` 照常判,无需特别适配。

    `rollout_id`(P4-2/P4-3,默认 None = 现状,裸滚不受影响):非空时本协调器除了写 rolling_task
    终态,还把同一终态回写到 rollouts 表 —— done/degraded 写终态不冻结;failed 置 frozen=True
    (失败即停留下半迁移态,标记「冻结待人工」)。为保证**每条终态出口**(无发现实例 / 某 agent
    list 失败 / total==0 / total<2 非 force / 灰度子集未命中 / graceful 失败即停 / 全成功 / 超时 /
    通用异常)都同步回写、不漏分支,把 finish_rolling 收敛进下方局部 `_finish` 闭包统一落两边。

    `instance_filter`(P5-2 灰度,默认 None = 全量,裸滚不受影响):containerId 集合;非 None 时只滚
    其中的实例子集(canary),其余不动。**关键不变式 —— 健康门按「全集」判定、滚动只滚「子集」**:
    - 集群健康门(total==0 / total<2 非 force)仍按**全量 targets** 判定 —— 灰度即便只点 1 个实例滚,
      也受全集保护(全集 healthy≥2 或 force 才放行),避免「全集就 2 个、灰度滚其一时另一个恰好挂掉」
      导致瞬时 0 健康。
    - 门通过后再按 instance_filter 过滤出实际要滚的 `roll_list`;落 nodes / 逐一滚 / 失败即停都基于
      `roll_list`(非全集)。degraded 标记仍按**全集** total<2(集群整体是否处于降级容量)。
    - instance_filter 非空但 roll_list 为空(指定的 containerId 都不在该服务健康实例内)→ failed。

    **等价回归保证**:`rollout_id=None`、`instance_filter=None` 且 `mode='restart'`(image 随之为
    None)时,本函数与改造前**逐行等价**(健康门、落 nodes、发 graceful-restart、失败即停、degraded、
    终态回写都不变;roll_list 退化为全量 targets,_finish 仅多一层透传,命令分流退化为 restart 分支)。
    既有 test_rolling.py 的 `_run_service_rolling` 用例(均不传 mode/image)复跑即验证此等价。

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

        # pull-redeploy 模式按 containerId 解析 compose 工程目录(dir):来源 = group 的发现行
        # (发现权威,非手配),{container_id: dir} 映射。container_id/dir 为空的行不入映射(无从定位)。
        # restart 模式不需要 dir,映射留空(逐行等价,不影响 graceful-restart 分支)。
        dir_by_container = {
            dn.container_id: dn.dir
            for dn in group
            if dn.container_id and dn.dir
        }

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

        # 3. 集群健康门(**按全集 targets 判定**,灰度子集亦受全集保护):0 → 无可滚;<2 且非 force →
        #    拒(零中断不可达)。灰度只滚子集也走这道全集门 —— 见函数 docstring「健康门按全集」不变式。
        total = len(targets)
        if total == 0:
            await _finish(
                "failed", error="无可滚实例(各 agent 均无 matched 且 healthy 的本机实例)")
            return
        if total < 2 and not force:
            await _finish(
                "failed", error=f"集群健康实例数={total}<2,无法零中断滚动;扩容或 force")
            return

        # 3.5 灰度子集过滤(P5-2):instance_filter=None → roll_list=全量 targets(裸滚等价);否则取
        #     containerId ∈ instance_filter 的子集。健康门已在上面按全集放行,这里只决定**实际滚哪些**。
        if instance_filter is None:
            roll_list = targets
        else:
            roll_list = [t for t in targets if t["containerId"] in instance_filter]
            if not roll_list:
                # 指定了灰度子集却无一命中该服务的健康实例(containerId 写错 / 实例已不在健康集)→ failed。
                await _finish(
                    "failed",
                    error="灰度子集未命中任何可滚实例(containerId 不在该服务健康实例内)")
                return

        # 4. 全局逐一滚:把 **roll_list**(全量或灰度子集)落 nodes(pending,带 agentId+address+containerId),
        #    再按序滚(全局一次只滚一个的不变式在子集内仍成立)。
        nodes = [{"agentId": t["agentId"], "address": t["address"],
                  "containerId": t["containerId"], "status": "pending"} for t in roll_list]
        await hub_state.update_rolling_nodes(task_id, nodes)
        for idx, node in enumerate(nodes):
            nodes[idx]["status"] = "in-progress"
            await hub_state.update_rolling_nodes(task_id, nodes)
            req = str(uuid.uuid4())
            # 命令分流(仅此处按 mode 分):restart → graceful-restart(原地重启);
            # pull-redeploy → graceful-redeploy(额外带 image + 该实例的 compose 目录 dir)。
            # 其余字段(requestId/containerId/healthBaseUrl/三个超时)两条命令完全相同。
            common = {
                "requestId": req,
                "containerId": node["containerId"],
                "healthBaseUrl": f"http://{node['address']}",
                "settleSec": settings.rolling_settle_sec,
                "shutdownTimeoutSec": settings.rolling_shutdown_timeout,
                "readyTimeoutSec": settings.rolling_ready_timeout,
            }
            if mode == "pull-redeploy":
                node_dir = dir_by_container.get(node["containerId"])
                if not node_dir:
                    # 无 dir 无法定位该实例 compose 工程 → 失败即停(余下标 skipped,与 graceful 失败同收敛)。
                    nodes[idx]["status"] = "failed"
                    nodes[idx]["error"] = "无法解析实例 compose 目录(dir)"
                    for n in nodes[idx + 1:]:
                        n["status"] = "skipped"
                    await _finish(
                        "failed", nodes=nodes,
                        error=f"节点 {node['agentId']}/{node['address']} 无法解析实例 compose 目录(dir)")
                    return
                msg = {"type": "graceful-redeploy", **common, "image": image, "dir": node_dir}
            else:
                msg = {"type": "graceful-restart", **common}
            res = await hub_state.call_agent(node["agentId"], msg, timeout=settings.rolling_cmd_timeout)
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

        # 5. 全部成功:**degraded 仍按全集 total<2 判定**(集群整体容量是否降级,与是否灰度无关;
        #    force 放行的小集群滚完标 degraded),否则 done。
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
