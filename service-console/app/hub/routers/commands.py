from __future__ import annotations
from app.config import settings as _hub_settings

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status

from app.hub import force_guard
from app.hub.api_support import _build_command_list_response, _command_list_query_dependency, _derive_requested_by, _require_admin_token, _serialize_command, get_command_events_response
from app.hub.models import CommandDispatchRequest, CommandDispatchResponse, CommandEventSnapshot, CommandListResponse, CommandSnapshot, ListInstancesRequest, ListInstancesResponse


router = APIRouter(tags=["命令管理"])


@router.get("/api/commands", response_model=CommandListResponse, summary="查询全局命令列表", description="分页查询所有 Agent 的命令历史，支持多条件筛选与排序。")
async def list_commands(
    query: dict[str, Any] = Depends(_command_list_query_dependency),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌", description="管理操作鉴权令牌。"),
) -> CommandListResponse:
    _require_admin_token(admin_token)
    return await _build_command_list_response(**query)


@router.get("/api/commands/{request_id}", response_model=CommandSnapshot, summary="查询单条命令", description="根据请求 ID 查询单条命令的最新状态。")
async def get_command(
    request_id: str = Path(title="请求 ID", description="要查询的命令请求 ID。"),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌", description="管理操作鉴权令牌。"),
) -> CommandSnapshot:
    _require_admin_token(admin_token)
    return await _serialize_command(request_id)


@router.get("/api/commands/{request_id}/events", response_model=list[CommandEventSnapshot], summary="查询命令事件", description="查询命令的完整审计事件流。")
async def get_command_events(
    request_id: str = Path(title="请求 ID", description="要查询事件流的命令请求 ID。"),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌", description="管理操作鉴权令牌。"),
) -> list[CommandEventSnapshot]:
    _require_admin_token(admin_token)
    return await get_command_events_response(request_id)


@router.post("/api/agents/{agent_id}/commands", response_model=CommandDispatchResponse, status_code=status.HTTP_202_ACCEPTED, summary="下发命令", description="向指定 Agent 下发 update 或 restart 命令。")
async def dispatch_command(
    request: CommandDispatchRequest,
    agent_id: str = Path(title="Agent 标识", description="要接收命令的 Agent 唯一标识。"),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌", description="管理操作鉴权令牌。"),
    requested_by_hint: str | None = Header(default=None, alias="X-Requested-By", title="请求发起方提示", description="调用方自报的发起方标识，仅作审计提示，requested_by 由 hub 据 admin token 服务端派生。"),
    request_source: str | None = Header(default=None, alias="X-Requested-Source", title="请求来源", description="调用来源，例如控制台、调度器。"),
) -> CommandDispatchResponse:
    import app.main as main_module

    _require_admin_token(admin_token)
    # 安全:requested_by 据 admin token 服务端派生强制覆盖,客户端 X-Requested-By 仅作 hint(记日志,不作权威)。
    requested_by = _derive_requested_by(admin_token)
    main_module.logger.info("命令下发授权身份=%s,客户端 X-Requested-By 提示=%s", requested_by, requested_by_hint)
    agent = await main_module.hub_state.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not agent["online"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is offline")

    # force stop 服务端护栏(仅 action=stop && mode=force):被拒命令不入库不下发(拒在记录前)。
    # 顺序(#6/#14):先过「最后健康实例」闸(及 #2 的 serviceName 必填),全部通过后才记速率账——
    # 只有真正将下发的 force stop 才占配额,避免被 ② 闸拒的尝试白白消耗配额、把窗口锁满误触 429。
    # ② 不可停掉某 service 最后一个健康实例(allowLastInstance 可显式跳过)。
    # ① 全局滑窗速率(置于 force 块末尾)。
    if request.action == "stop" and request.mode == "force":
        if not request.allow_last_instance:
            # #2 fail-closed:缺 serviceName 无法核实最后健康实例,直接拒(不再无声跳过该安全闸)。
            if not request.service_name:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="force stop 须提供 serviceName 以核实最后健康实例,或显式 allowLastInstance",
                )
            await force_guard.check_last_healthy_instance(
                main_module.hub_state,
                agent_id,
                request.service_name,
                # #13:用短超时(远小于 BFF 15s),不复用 rolling_cmd_timeout(480s)。
                timeout=_hub_settings.list_instances_timeout,
            )
        # allowLastInstance=true 跳过 ② 后仍走 ① 速率闸;② 通过后也在此统一记账。
        force_guard.check_force_rate_limit(_hub_settings)

    payload = {
        "type": "command",
        "requestId": request.request_id,
        "action": request.action,
        "dir": request.dir,
    }
    if request.mode:
        payload["mode"] = request.mode
    if request.image:
        payload["image"] = request.image
    # 优雅 drain 入参透传(仅 graceful stop/pull-redeploy 用;agent 读 healthBaseUrl 调 worker drain)。
    if request.healthBaseUrl:
        payload["healthBaseUrl"] = request.healthBaseUrl
    if request.shutdownTimeoutSec is not None:
        payload["shutdownTimeoutSec"] = request.shutdownTimeoutSec

    await main_module.hub_state.store_command(
        agent_id,
        payload,
        requested_by=requested_by,
        request_source=request_source,
    )

    websocket = await main_module.hub_state.get_connection(agent_id)
    if websocket is None:
        await main_module.hub_state.mark_result(request.request_id, "failed", error="Agent connection is unavailable")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent connection is unavailable")

    try:
        await websocket.send_json(payload)
        main_module.logger.info("Dispatched command %s to agent %s", request.request_id, agent_id)
    except Exception as exc:
        main_module.logger.exception("Failed to dispatch command %s to agent %s", request.request_id, agent_id)
        await main_module.hub_state.mark_result(request.request_id, "failed", error=f"Failed to dispatch command: {exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to dispatch command") from exc

    command = await _serialize_command(request.request_id)
    return CommandDispatchResponse(accepted=True, command=command)


@router.post(
    "/api/agents/{agent_id}/list-instances",
    response_model=ListInstancesResponse,
    summary="查询 Agent 实例",
    description="经指定 Agent 查询某 service 当前的容器实例(含健康状态),供平台节点页展示健康实例数。",
    tags=["命令管理"],
)
async def list_agent_instances(
    request: ListInstancesRequest,
    agent_id: str = Path(title="Agent 标识", description="要查询实例的 Agent 唯一标识。"),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌", description="管理操作鉴权令牌。"),
) -> ListInstancesResponse:
    import app.main as main_module

    # 安全:节点控制查端点不可匿名,首行强制校验 admin token(非法即 403)。
    _require_admin_token(admin_token)

    agent = await main_module.hub_state.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not agent["online"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is offline")

    req = str(uuid.uuid4())
    message: dict[str, Any] = {
        "type": "list-instances",
        "requestId": req,
        "serviceName": request.serviceName,
    }
    # 仅在显式给出时透传 expectedComposeProject,空值不注入(与 dispatch 的可选字段风格一致)。
    if request.expectedComposeProject:
        message["expectedComposeProject"] = request.expectedComposeProject

    try:
        result = await main_module.hub_state.call_agent(
            agent_id,
            message,
            # #13:短超时,远小于 BFF dispatch 的 httpx 15s,避免边界超时致"已执行却显失败"。
            timeout=_hub_settings.list_instances_timeout,
        )
    except asyncio.TimeoutError as exc:
        # agent 未在超时内应答;脱敏,不向调用方暴露内部细节。
        main_module.logger.warning("Agent %s 未应答 list-instances(requestId=%s)", agent_id, req)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="agent 未应答 list-instances") from exc
    except RuntimeError as exc:
        # check→call 竞态:online 判定通过后、发送前 agent 恰好断开,call_agent 抛 RuntimeError。
        # 归一化为脱敏 502(对齐「失败一律 502」契约,不让 RuntimeError 逃逸成未脱敏 500)。
        main_module.logger.warning("Agent %s 连接不可用,list-instances 失败(requestId=%s)", agent_id, req)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="agent 连接不可用") from exc

    if result.get("status") != "success":
        # agent 端执行失败,回传其 error(脱敏:仅取 error 字段)。
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=result.get("error") or "agent list-instances 失败")

    return ListInstancesResponse(status="success", instances=result.get("instances") or [])


@router.post("/api/commands/{request_id}/retry", response_model=CommandDispatchResponse, status_code=status.HTTP_202_ACCEPTED, summary="重试失败命令", description="重新下发一条失败命令，并生成新的请求 ID。")
async def retry_command(
    request_id: str = Path(title="请求 ID", description="要重试的失败命令请求 ID。"),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌", description="管理操作鉴权令牌。"),
    requested_by_hint: str | None = Header(default=None, alias="X-Requested-By", title="请求发起方提示", description="调用方自报的发起方标识，仅作审计提示，requested_by 由 hub 据 admin token 服务端派生。"),
    request_source: str | None = Header(default=None, alias="X-Requested-Source", title="请求来源", description="调用来源，例如控制台、调度器。"),
) -> CommandDispatchResponse:
    import app.main as main_module

    _require_admin_token(admin_token)
    # 安全:同 dispatch,重试生成的新命令 requested_by 据 admin token 派生,客户端 X-Requested-By 仅作 hint。
    requested_by = _derive_requested_by(admin_token)
    main_module.logger.info("命令重试授权身份=%s,客户端 X-Requested-By 提示=%s", requested_by, requested_by_hint)
    original = await main_module.hub_state.get_command(request_id)
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")
    if original["status"] != "failed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only failed commands can be retried")

    agent = await main_module.hub_state.get_agent(original["agent_id"])
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not agent["online"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is offline")

    retried = await main_module.hub_state.retry_command(
        request_id,
        requested_by=requested_by,
        request_source=request_source,
    )
    if retried is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")

    _, retry_record = retried
    websocket = await main_module.hub_state.get_connection(retry_record["agent_id"])
    if websocket is None:
        await main_module.hub_state.mark_result(retry_record["request_id"], "failed", error="Agent connection is unavailable")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent connection is unavailable")

    try:
        await websocket.send_json(retry_record["payload"])
        main_module.logger.info("Retried command %s as %s for agent %s", request_id, retry_record["request_id"], retry_record["agent_id"])
    except Exception as exc:
        main_module.logger.exception("Failed to retry command %s for agent %s", request_id, retry_record["agent_id"])
        await main_module.hub_state.mark_result(retry_record["request_id"], "failed", error=f"Failed to dispatch command: {exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to dispatch command") from exc

    return CommandDispatchResponse(accepted=True, command=await _serialize_command(retry_record["request_id"]))