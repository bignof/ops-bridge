"""投放(Rollout)路由(P4-2 发布→投放触发链 + P4-3 失败即停 freeze + 运行记录)。

设计取舍 —— **显式投放(desired-state)而非「改 active 即自动滚」**:发布 / 切版 / 切镜像只改 DB
(本模块**不动** releases / services / service-plugins 的写端点),漂移在实例页 / 对账可见,由人经
本组端点显式发起一次「投放」把 desired-state 推到运行实例(或回滚)。一次投放 = 一条 rollouts 记录
(运行态)+ 一条底层 rolling_task(跨机顺序滚动的逐实例进度),二者经 rollout.rolling_task_id 关联。

鉴权:全部走 hub 自带的 X-Admin-Token(与 /api/service-rolling 同款),已在 SessionGuard 白名单
放行(见 middleware WHITELIST 的 `/api/rollouts`),由各端点首行 `_require_admin_token` 把关。

并发互斥:复用 rolling_tasks.active_key(= serviceName)的 UNIQUE —— 同一 nacos 服务同时只允许一次
投放(撞锁 → 409)。**先建 rolling task 成功、再建 rollout**,保证撞锁时不留半条 running 的孤儿
rollout(顺序见 `_start_rollout`)。

本期边界:
- `mode='pull-redeploy'` 协调器尚不支持(只发 graceful-restart),路由层 **422 占位**(列保留两值)。
- rollback **半实现**:本期 restart 模式下「回滚」与「重试」机制同构(都按相同 serviceName 重跑一轮
  滚动);真正「把 active 切回上一版」的版本切换在后续 P4-4/P4-5 落地,届时调用方建 rollout 时传
  previous_target。本模块只实现**状态机 + 入口**,不碰 releases / 版本表。
"""

from __future__ import annotations

from app.config import settings as _hub_settings
import asyncio
import math
import uuid

from fastapi import APIRouter, Header, HTTPException, Path, Query, status

from app import store
from app.hub.api_support import _require_admin_token
from app.hub.store import RollingConflict
from app.models import RolloutCreateIn, RolloutDetailOut, RolloutListOut, RolloutOut
# 复用 rolling.py 的后台任务台账 + 收尾回调 + 跨机协调器(同一份,不另立一套)。
from app.hub.routers.rolling import (
    _CROSS_AGENT_SENTINEL,
    _background,
    _on_task_done,
    _run_service_rolling,
)


router = APIRouter(prefix="/api/rollouts", tags=["投放"])


async def _start_rollout(
    *,
    service_name: str,
    namespace: str | None,
    mode: str,
    force: bool,
    target: str | None,
    previous_target: str | None,
    trigger: str,
) -> dict:
    """内部 helper:建锁 → 建 rollout → 后台跑跨机滚动,返回 `{rolloutId, taskId}`。

    POST / retry / rollback 三个入口共用本函数(retry/rollback 只是换 trigger + 复用原 rollout
    的 serviceName/namespace/mode/force/previousTarget)。

    顺序保证(关键):**先 create_rolling_task 成功、再 create_rollout**。create_rolling_task 撞
    active_key UNIQUE 会抛 RollingConflict(调用方映射 409),此时尚未建 rollout → 不留半条 running
    的孤儿 rollout 记录。建完 rollout 再起后台任务,把 rollout_id 透传给协调器,由其在每个终态出口
    回写 rollout(done/degraded 不冻结;failed → frozen=True)。
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

    # 3. 后台跑跨机顺序滚动协调器,透传 rollout_id(终态由协调器回写两边)。复用 rolling.py 的
    #    _background 集合 + _on_task_done 收尾,与 /api/service-rolling 同构。
    bg = asyncio.create_task(
        _run_service_rolling(
            task_id, service_name, force, main_module.hub_state, _hub_settings, rollout_id=rollout_id))
    _background.add(bg)
    bg.add_done_callback(_on_task_done)

    return {"rolloutId": rollout_id, "taskId": task_id}


@router.post(
    "",
    summary="发起投放",
    description="显式投放:把 desired-state 推到运行实例(本期走跨机 graceful-restart)。返回 {rolloutId, taskId}。",
)
async def create_rollout_endpoint(
    body: RolloutCreateIn,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
):
    _require_admin_token(admin_token)
    # mode 非 restart 本期协调器不支持(只发 graceful-restart),显式 422 占位,别假装能跑。
    if body.mode != "restart":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pull-redeploy 投放将于后续批次接入(当前仅支持 restart)")
    try:
        result = await _start_rollout(
            service_name=body.service_name,
            namespace=body.namespace,
            mode=body.mode,
            force=body.force,
            target=body.target,
            previous_target=None,  # 首次投放无上一版参考(回滚参考由 rollback 入口按需带)
            trigger=body.trigger,
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
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
    namespace: str | None = Query(default=None, title="按命名空间过滤"),
    service_name: str | None = Query(default=None, alias="serviceName", title="按服务名过滤"),
    status_filter: str | None = Query(default=None, alias="status", title="按状态过滤"),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
) -> RolloutListOut:
    _require_admin_token(admin_token)
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
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
) -> RolloutDetailOut:
    _require_admin_token(admin_token)
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
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
):
    _require_admin_token(admin_token)
    original = await asyncio.to_thread(store.get_rollout, rollout_id)
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="投放记录不存在")
    if original.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"仅 failed 投放可重试(当前状态 {original.status})")
    try:
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
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌"),
):
    _require_admin_token(admin_token)
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
    try:
        # 回滚目标 = 上一版;故新 rollout 的 target 用原 previous_target(语义:把现状推回上一版)。
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
