"""投放(Rollout)路由(P4-2 发布→投放触发链 + P4-3 失败即停 freeze + 运行记录)。

设计取舍 —— **显式投放(desired-state)而非「改 active 即自动滚」**:发布 / 切版 / 切镜像只改 DB
(本模块**不动** releases / services / service-plugins 的写端点),漂移在实例页 / 对账可见,由人经
本组端点显式发起一次「投放」把 desired-state 推到运行实例(或回滚)。一次投放 = 一条 rollouts 记录
(运行态)+ 一条底层 rolling_task(跨机顺序滚动的逐实例进度),二者经 rollout.rolling_task_id 关联。

鉴权:走平台 JWT(`Depends(require_session)`,与 /api/services、/api/nodes 等 SPA 直调端点同款)。
SPA(Rollout 记录页 / 发布弹窗)只持 Bearer JWT 直调本组端点,故**不**用 hub 的 X-Admin-Token
(SPA 拿不到它)。`/api/rollouts/**` 走 `/api/**` 的 default-deny JWT 中间件(已从 middleware
WHITELIST 移除),端点内 `Depends(require_session)` 作纵深防御(双层)。注:间接的跨机滚动入口
`/api/rolling-restart`、`/api/service-rolling` 仍走 admin-token(它们由 nodes.py 的 JWT BFF 转调,
非 SPA 直调),不在本次改动范围。

并发互斥:复用 rolling_tasks.active_key(= serviceName)的 UNIQUE —— 同一 nacos 服务同时只允许一次
投放(撞锁 → 409)。**先建 rolling task 成功、再建 rollout**,保证撞锁时不留半条 running 的孤儿
rollout(顺序见 `_start_rollout`)。

本期边界:
- `mode`:支持 'restart'(逐实例 graceful-restart 原地重启)与 'pull-redeploy'(逐实例
  graceful-redeploy 滚动重拉镜像,须带 image);其它 mode → 422。pull-redeploy 的 image **不落库**
  (rollouts 表无 image 列,本期取舍),故 retry/rollback 对 pull-redeploy 原投放一律 422 拒(取不回
  image,引导前端重走发布弹窗);restart 原投放的 retry/rollback 照常重跑。
- rollback **半实现**:本期 restart 模式下「回滚」与「重试」机制同构(都按相同 serviceName 重跑一轮
  滚动);真正「把 active 切回上一版」的版本切换在后续 P4-4/P4-5 落地,届时调用方建 rollout 时传
  previous_target。本模块只实现**状态机 + 入口**,不碰 releases / 版本表。
"""

from __future__ import annotations

from app.config import settings as _hub_settings
import asyncio
import logging
import math
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app import store
from app.auth import require_session
from app.db_models import Service
from app.hub.store import RollingConflict
from app.models import RolloutCreateIn, RolloutDetailOut, RolloutListOut, RolloutOut
# 复用 rolling.py 的后台任务台账 + 收尾回调 + 跨机协调器(同一份,不另立一套)。
from app.hub.routers.rolling import (
    _CROSS_AGENT_SENTINEL,
    _background,
    _on_task_done,
    _run_service_rolling,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rollouts", tags=["投放"])


async def _prewarm_service(service_name: str, hub_state, settings) -> None:
    """restart 投放前的 best-effort「预热」:通知承载该服务的各 agent 把插件回源下载入缓存。

    **为什么只 restart 预热**:restart 模式下 worker 优雅重启时会**从 agent 缓存重新拉插件**——
    预先让 agent 把插件回源下载入缓存,随后重启就能零等待拉包。pull-redeploy 拉的是**镜像**(不经
    agent 插件缓存),预热插件无意义,故调用方只在 mode=='restart' 时调本函数(见 `_start_rollout`)。

    **best-effort 铁律**:本函数任何失败 / 超时 / 异常**绝不**冒泡、绝不影响投放——预热纯属加速,
    预热没热完(或全挂)时 worker 重启会按需回源拉取,无害。故:① 每个 agent 的 call_agent 单独包
    try/except(含 asyncio.TimeoutError 与任意 Exception),失败只记一条日志、继续下一个 agent;
    ② 整个函数再包一层兜底 try/except,任何意外(聚合查询 / Service 查询出错等)都只记日志。
    用短超时(list_instances_timeout,非长的 rolling_cmd_timeout):预热是旁路加速,不该久等。

    serviceCode 解析:console 这边 service_name 是 nacos 服务名,而 agent 的 prewarm 契约要的是
    **平台侧 serviceCode**(= Service.service_code)。按 `nacos_service_name == service_name` 查
    Service 行,收集去重其全部 service_code(同一 nacos 名可能跨多 namespace 有多行 → 多个 code,
    一并发给 agent)。查不到任何映射 → 无从预热,静默 return。
    """
    try:
        # 1. 定位 agents:按 nacos 名聚合 active 发现实例 → 取承载该服务的不重复 agent(与滚动协调器同源)。
        agg = await asyncio.to_thread(store.aggregate_discovered_by_nacos, "active")
        group = agg.get(service_name, [])
        agent_ids: list[str] = []
        for dn in group:
            if dn.agent_id not in agent_ids:
                agent_ids.append(dn.agent_id)
        if not agent_ids:
            # 无承载该服务的活跃 agent → 无对象可预热,静默跳过(投放本身不受影响)。
            return

        # 2. 解析 serviceCodes:nacos_service_name == service_name 的 Service 行,去重收集 service_code。
        services = await asyncio.to_thread(
            store.find_rows, Service,
            filters=[Service.nacos_service_name == service_name])
        service_codes: list[str] = []
        for svc in services:
            if svc.service_code and svc.service_code not in service_codes:
                service_codes.append(svc.service_code)
        if not service_codes:
            # nacos 名映射不到任何 serviceCode → 无从预热(agent 按 serviceCode 取插件清单),静默跳过。
            return

        # 3. 逐 agent best-effort 发 prewarm;单个 agent 失败/超时只记日志、继续下一个,绝不抛。
        for agent_id in agent_ids:
            try:
                await hub_state.call_agent(
                    agent_id,
                    {"type": "prewarm", "requestId": str(uuid.uuid4()), "services": service_codes},
                    timeout=settings.list_instances_timeout)
            except Exception as exc:  # noqa: BLE001 —— 含 TimeoutError;best-effort,吞掉继续下一个 agent
                logger.info(
                    "预热(prewarm)agent %s 失败(best-effort,忽略,不影响投放): %s", agent_id, exc)
    except Exception as exc:  # noqa: BLE001 —— 兜底:聚合/Service 查询等任何意外都不得影响投放
        logger.warning("预热(prewarm)服务 %s 出现意外(best-effort,忽略): %s", service_name, exc)


async def _start_rollout(
    *,
    service_name: str,
    namespace: str | None,
    mode: str,
    force: bool,
    target: str | None,
    previous_target: str | None,
    trigger: str,
    instances: list[str] | None = None,
    image: str | None = None,
) -> dict:
    """内部 helper:建锁 → 建 rollout → 后台跑跨机滚动,返回 `{rolloutId, taskId}`。

    POST / retry / rollback 三个入口共用本函数(retry/rollback 只是换 trigger + 复用原 rollout
    的 serviceName/namespace/mode/force/previousTarget)。

    顺序保证(关键):**先 create_rolling_task 成功、再 create_rollout**。create_rolling_task 撞
    active_key UNIQUE 会抛 RollingConflict(调用方映射 409),此时尚未建 rollout → 不留半条 running
    的孤儿 rollout 记录。建完 rollout 再起后台任务,把 rollout_id 透传给协调器,由其在每个终态出口
    回写 rollout(done/degraded 不冻结;failed → frozen=True)。

    `instances`(P5-2 灰度,默认 None = 全量):containerId 列表;非空时转成 set 作 instance_filter
    透传给协调器 → 只滚该子集(健康门仍按全集判定,见 `_run_service_rolling` 不变式)。retry/rollback
    入口本期**不带** instances(整体重滚,见各入口注释);子集信息本期不持久化到 rollouts 行。

    `mode` / `image`(零中断滚动重拉镜像):mode='pull-redeploy' 时协调器逐实例发 graceful-redeploy
    并带 image(目标镜像)。**rollouts 表本期无 image 列**,故 image 只随本次内存调用透传给协调器,
    不落库 —— retry/rollback 重跑 pull-redeploy 时取不回 image(由各入口按取舍处理,见其注释)。
    """
    import app.main as main_module

    task_id = str(uuid.uuid4())
    rollout_id = str(uuid.uuid4())

    # 1. 先抢锁建 rolling task(哨兵 agent="*",锁键=serviceName,与 POST /api/service-rolling 同款)。
    #    撞锁 → RollingConflict 冒泡给调用方转 409;此刻还没建 rollout,无孤儿。
    await main_module.hub_state.create_rolling_task(
        task_id, _CROSS_AGENT_SENTINEL, service_name, force, active_key=service_name)

    # 2. 锁拿到后才建 rollout(status='running'),并预写 rolling_task_id 关联。
    await asyncio.to_thread(
        store.create_rollout,
        rollout_id=rollout_id,
        service_name=service_name,
        namespace=namespace,
        mode=mode,
        trigger=trigger,
        target=target,
        previous_target=previous_target,
        force=force,
        rolling_task_id=task_id,
    )

    # 2.5 best-effort 预热(仅 restart):投放前通知各 agent 预拉插件缓存,使随后 worker 重启拉包零等待。
    #     选 (a) 并行后台任务(非 await):预热在 agent 端是幂等的回源下载,worker 重启时若还没热完就
    #     按需拉,无害;放后台则投放零延迟、且预热失败不可能改变本函数返回/时序(best-effort 铁律)。
    #     与滚动协调器并行起,同样进 _background + _on_task_done 收尾(避免「task was destroyed」+ 吞异常)。
    #     pull-redeploy 拉的是镜像非插件,不预热(见 _prewarm_service docstring)。
    if mode == "restart":
        warm = asyncio.create_task(
            _prewarm_service(service_name, main_module.hub_state, _hub_settings))
        _background.add(warm)
        warm.add_done_callback(_on_task_done)

    # 3. 后台跑跨机顺序滚动协调器,透传 rollout_id(终态由协调器回写两边)+ instance_filter(灰度子集,
    #    空列表/None → None = 全量)。复用 rolling.py 的 _background 集合 + _on_task_done 收尾,
    #    与 /api/service-rolling 同构。
    bg = asyncio.create_task(
        _run_service_rolling(
            task_id, service_name, force, main_module.hub_state, _hub_settings,
            rollout_id=rollout_id, instance_filter=set(instances) if instances else None,
            mode=mode, image=image))
    _background.add(bg)
    bg.add_done_callback(_on_task_done)

    return {"rolloutId": rollout_id, "taskId": task_id}


@router.post(
    "",
    summary="发起投放",
    description="显式投放:把 desired-state 推到运行实例(mode=restart 走跨机 graceful-restart;mode=pull-redeploy 走跨机 graceful-redeploy 并须带 image)。返回 {rolloutId, taskId}。",
)
async def create_rollout_endpoint(
    body: RolloutCreateIn,
    _: str = Depends(require_session),
):
    # mode 只允许 restart(原地重启)| pull-redeploy(滚动重拉镜像);其它值 → 422。
    if body.mode not in ("restart", "pull-redeploy"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"不支持的投放模式: {body.mode}(仅支持 restart | pull-redeploy)")
    # pull-redeploy 必须指定目标镜像(协调器据此逐实例 graceful-redeploy);缺 image → 422。
    if body.mode == "pull-redeploy" and not body.image:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pull-redeploy 投放须指定 image")
    try:
        result = await _start_rollout(
            service_name=body.service_name,
            namespace=body.namespace,
            mode=body.mode,
            force=body.force,
            target=body.target,
            previous_target=None,  # 首次投放无上一版参考(回滚参考由 rollback 入口按需带)
            trigger=body.trigger,
            instances=body.instances,  # P5-2:非空 → 灰度只滚该子集;空/缺省 → 全量
            image=body.image,  # pull-redeploy 的目标镜像;restart 时为 None(协调器忽略)
        )
    except RollingConflict as exc:
        # 同服务已有投放占锁(active_key=serviceName)→ 409;此时未建 rollout,无孤儿。
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同服务投放进行中") from exc
    return result


@router.get(
    "",
    response_model=RolloutListOut,
    summary="投放记录列表",
    description="分页返回投放记录(createdAt 倒序);支持 namespace / serviceName / status 过滤。",
)
async def list_rollouts_endpoint(
    _: str = Depends(require_session),
    namespace: str | None = Query(default=None, title="按命名空间过滤"),
    service_name: str | None = Query(default=None, alias="serviceName", title="按服务名过滤"),
    status_filter: str | None = Query(default=None, alias="status", title="按状态过滤"),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
) -> RolloutListOut:
    result = await asyncio.to_thread(
        store.list_rollouts,
        namespace=namespace,
        service_name=service_name,
        status=status_filter,
        page=page,
        page_size=page_size,
    )
    count = result["total"]
    return RolloutListOut(
        count=count,
        rows=[RolloutOut.model_validate(row) for row in result["rows"]],
        page=page,
        page_size=page_size,
        total_page=math.ceil(count / page_size) if page_size else 0,
    )


@router.get(
    "/{rollout_id}",
    response_model=RolloutDetailOut,
    summary="投放详情(含滚动进度)",
    description="单条投放;若关联 rolling task,嵌入其 nodes/status 作 rollingTask 便于看逐实例进度。",
)
async def get_rollout_endpoint(
    rollout_id: str = Path(...),
    _: str = Depends(require_session),
) -> RolloutDetailOut:
    record = await asyncio.to_thread(store.get_rollout, rollout_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="投放记录不存在")
    detail = RolloutDetailOut.model_validate(record)
    # 嵌入底层滚动逐实例进度(已是 camelCase 的 nodes/status 等);无关联或查不到则留 None。
    if record.rolling_task_id:
        import app.main as main_module

        detail.rolling_task = await main_module.hub_state.get_rolling_task(record.rolling_task_id)
    return detail


@router.post(
    "/{rollout_id}/retry",
    summary="重试投放",
    description="仅 status=failed 可重试;新建一条 trigger=retry 的投放,按相同 serviceName/namespace/mode/force 重跑。返回 {rolloutId, taskId}。",
)
async def retry_rollout_endpoint(
    rollout_id: str = Path(...),
    _: str = Depends(require_session),
):
    original = await asyncio.to_thread(store.get_rollout, rollout_id)
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="投放记录不存在")
    if original.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"仅 failed 投放可重试(当前状态 {original.status})")
    # pull-redeploy 重跑需 image,但 rollouts 表无 image 列(本期取舍,不为此加列)→ 取不回 →
    # 拒,引导前端重新走发布弹窗发起一次带 image 的 POST。restart 模式无此约束,照常重跑。
    if original.mode == "pull-redeploy":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pull-redeploy 重试需重新指定 image,请走发布弹窗")
    try:
        # P5-2:retry 本期**不带** instances(整体重滚整个服务,不复用原投放的灰度子集)——
        # rollouts 行未持久化子集信息,且失败重试通常想把整服务推到一致;灰度子集如需重滚由前端
        # 重新发起一次带 instances 的 POST /api/rollouts。
        result = await _start_rollout(
            service_name=original.service_name,
            namespace=original.namespace,
            mode=original.mode,
            force=original.force,
            target=original.target,
            previous_target=original.previous_target,
            trigger="retry",
        )
    except RollingConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同服务投放进行中") from exc
    return result


@router.post(
    "/{rollout_id}/rollback",
    summary="回滚投放",
    description="仅 status=failed 且有 previousTarget 可回滚;新建 trigger=rollback 的投放重跑(本期 restart 模式下与重试同构)。返回 {rolloutId, taskId}。",
)
async def rollback_rollout_endpoint(
    rollout_id: str = Path(...),
    _: str = Depends(require_session),
):
    original = await asyncio.to_thread(store.get_rollout, rollout_id)
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="投放记录不存在")
    if original.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"仅 failed 投放可回滚(当前状态 {original.status})")
    # 本期半实现:回滚需有上一版人读摘要(previous_target)作参考;无则拒(真正的版本切换待后续批次)。
    if not original.previous_target:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="无上一版可回滚(previousTarget 为空)")
    # pull-redeploy 回滚同样需 image 而表无该列(取舍同 retry)→ 拒,引导走发布弹窗指定上一版镜像。
    if original.mode == "pull-redeploy":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pull-redeploy 回滚需重新指定 image,请走发布弹窗")
    try:
        # 回滚目标 = 上一版;故新 rollout 的 target 用原 previous_target(语义:把现状推回上一版)。
        # P5-2:rollback 同样**不带** instances(整体回滚整服务,理由同 retry)。
        result = await _start_rollout(
            service_name=original.service_name,
            namespace=original.namespace,
            mode=original.mode,
            force=original.force,
            target=original.previous_target,
            previous_target=original.target,  # 回滚后的「上一版」即回滚前的当前 target
            trigger="rollback",
        )
    except RollingConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同服务投放进行中") from exc
    return result
