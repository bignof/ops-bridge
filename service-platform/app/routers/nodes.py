"""节点聚合路由(Task 9b)。

`GET /api/nodes`:节点页**权威源 = 平台 Service 表**(每行 = (agent×service)),组合三处:
① Service 表静态(`dir`/`defaultImage`/`nacosServiceName` + LEFT JOIN namespace.code →
`agentId`/`namespaceCode`);② hub 实时在线态(`hub_client.list_agents` 一次性拉全);
③ 健康实例数(`hub_client.list_instances` per-(agent,service) 并发 fan-out)。

约束 / 不变式:
- **响应全 camelCase**(评审 H2):`response_model=NodeListOut`,经 `*Out` 序列化,不手搓 dict。
- **纵深防御**:`Depends(require_session)`;`/api/` 前缀下 default-deny 中间件已先挡无/坏 JWT。
- **HUB_ADMIN_TOKEN 服务端注入**:hub 调用全经 `hub_client`(其 `_headers()` 注入 X-Admin-Token),
  绝不下发浏览器。
- **单 agent 卡死/超时不拖垮整页**(核心):`list_instances` 短超时 + `asyncio.gather(...,
  return_exceptions=True)` 收集异常 → 该行 `degraded=True、healthyCount=None`,其余行照常、整页 200。
- **降级矩阵**:
  - online 且有 nacosServiceName → fan-out:成功(dict 且 status==success)→ healthyCount=健康数、
    degraded=False;异常 / status!=success → healthyCount=None、degraded=True。
  - 离线行(agent 不在 list_agents map 或其 online 为假)/ 无 nacosServiceName → 不 fan-out:
    healthyCount=None、degraded=False。
  - `list_agents` 整体失败 → map 退化为空 → 全部行按离线处理(不崩整页)。

打桩说明:hub 调用经**模块引用** `hub_client.list_agents(...)` / `hub_client.list_instances(...)`,
故测试 `monkeypatch.setattr(nodes.hub_client, "list_agents", ...)` 能生效(同 namespaces 的 H7 打桩)。
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app import hub_client, store
from app.auth import require_session
from app.db_models import Namespace, Service
from app.models import (
    NodeActionIn,
    NodeActionOut,
    NodeListOut,
    NodeOperationOut,
    NodeOperationsListOut,
    NodeOut,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/nodes", tags=["节点控制"])
# 操作审计端点路径是 `/api/node-operations`(与 `/api/nodes` 平级,非其子路径),故单列一个
# 无 prefix 的 router 承载,由 main.py 一并 include。仍在 `/api/` 前缀下 → SessionGuard
# default-deny 照样覆盖(无/坏 JWT 401)。
operations_router = APIRouter(tags=["节点控制"])

# 操作审计 output / error 单字段截断上限:超过则保留**末尾** N 字符(结果通常在尾部),
# 加前缀标记,避免审计列表整页 payload 膨胀(评审冲突 2,用户定 D)。
_AUDIT_FIELD_MAX = 1000

# SELECT 列:Service 本行字段 + LEFT JOIN 回 namespace.code(label=namespace_code → camel namespaceCode）。
_LIST_COLUMNS = (
    Service.id,
    Service.namespace_id,
    Service.service_code,
    Service.dir,
    Service.default_image,
    Service.nacos_service_name,
    Namespace.code.label("namespace_code"),
)


def _load_agents_map() -> dict[str, dict[str, Any]]:
    """一次性拉 hub 在线态,转成 `{agentId: snapshot}`。

    `list_agents` 失败(配置缺失 / hub 不可达 / 非 2xx / 响应异常)→ **退化为空 map**,
    所有行据此按「离线/未知」处理,**绝不让整页 500**(核心不变式)。
    """
    try:
        agents = hub_client.list_agents()
    except Exception as exc:  # noqa: BLE001 —— hub 任意失败都不得拖垮整页,统一退化为空 map
        # 退化不崩页,但留一条脱敏告警(仅异常类型名,绝不记 token/完整消息),便于运维区分
        # 「hub 全程不可达」与「确实全部离线」。
        logger.warning("加载 hub agents 失败,节点页降级为全离线: %s", type(exc).__name__)
        return {}
    return {a.get("agentId"): a for a in agents if isinstance(a, dict) and a.get("agentId")}


def _healthy_count(result: Any) -> int | None:
    """从一条 list_instances 结果算健康实例数;非法/失败结果回 None(由调用方标 degraded)。

    成功契约:`dict` 且 `status == "success"` → 统计 `instances` 中 `healthy` 为真者;
    其它(异常对象 / 非 dict / status != success)→ None。
    """
    if isinstance(result, dict) and result.get("status") == "success":
        instances = result.get("instances") or []
        return len([i for i in instances if isinstance(i, dict) and i.get("healthy")])
    return None


@router.get(
    "",
    response_model=NodeListOut,
    summary="节点列表",
    description="以平台 Service 表为权威源分页返回 (agent×service) 节点;叠加 hub 实时在线态与健康实例数;单 agent 超时只标该行 degraded,不阻塞整页。",
)
async def list_nodes(
    _: str = Depends(require_session),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
) -> NodeListOut:
    # ① Service 表静态行(LEFT JOIN 回 namespaceCode);rows 是 dict(键 = 列 label/名)。
    rows, count = store.list_rows_joined(
        Service,
        columns=_LIST_COLUMNS,
        outer_joins=[(Namespace, Namespace.id == Service.namespace_id)],
        page=page,
        page_size=page_size,
    )

    # ② hub 在线态(一次性);失败退化为空 map → 全部行按离线处理。
    agents_map = _load_agents_map()

    # 标注每行在线态,并挑出「online 且有 nacosServiceName」的行做并发 fan-out。
    online_flags: list[bool] = []
    fanout_idx: list[int] = []  # 需 fan-out 的行在 rows 中的下标
    for i, row in enumerate(rows):
        agent = agents_map.get(row.get("namespace_code"))
        online = bool(agent and agent.get("online"))
        online_flags.append(online)
        if online and row.get("nacos_service_name"):
            fanout_idx.append(i)

    # ③ 并发 fan-out:to_thread 把 sync httpx 包成可并发协程,return_exceptions 收集每行异常
    #    (单 agent/服务超时只落到对应结果,不冒泡;短 timeout 由 hub_client.list_instances 默认值保证)。
    fanout_results: list[Any] = []
    if fanout_idx:
        fanout_results = await asyncio.gather(
            *[
                asyncio.to_thread(
                    hub_client.list_instances,
                    agents_map[rows[i]["namespace_code"]]["agentId"],
                    rows[i]["nacos_service_name"],
                )
                for i in fanout_idx
            ],
            return_exceptions=True,
        )
    # 行下标 → 该行的 healthyCount / degraded(默认:不 fan-out 的行 healthyCount=None、degraded=False)。
    health_by_idx: dict[int, int | None] = {}
    degraded_by_idx: dict[int, bool] = {}
    for idx, result in zip(fanout_idx, fanout_results):
        count_or_none = _healthy_count(result)
        health_by_idx[idx] = count_or_none
        # 异常(超时/HTTP错/任意 Exception)或 status!=success(_healthy_count 回 None)→ 该行 degraded。
        degraded_by_idx[idx] = count_or_none is None

    out_rows: list[NodeOut] = []
    for i, row in enumerate(rows):
        agent = agents_map.get(row.get("namespace_code"))
        out_rows.append(
            NodeOut(
                agent_id=row.get("namespace_code") or "",
                service_code=row["service_code"],
                namespace_code=row.get("namespace_code"),
                dir=row.get("dir"),
                default_image=row.get("default_image"),
                nacos_service_name=row.get("nacos_service_name"),
                online=online_flags[i],
                last_seen=agent.get("lastSeenAt") if agent else None,
                healthy_count=health_by_idx.get(i),
                degraded=degraded_by_idx.get(i, False),
            )
        )

    return NodeListOut(
        count=count,
        rows=out_rows,
        page=page,
        page_size=page_size,
        total_page=math.ceil(count / page_size) if page_size else 0,
    )


# =====================================================================================
# Task 10b:节点操作下发 + 操作审计
# =====================================================================================
#
# 寻址权威源 = 平台 Service 表:agentId(=namespace.code)+ serviceCode 反查得
# dir / nacosServiceName / defaultImage。**BFF 绝不接受客户端传路径或任意 image**——
# 这些一律由台账派生(防越权操作非授权目录 / 拉非白名单镜像)。requested_by 由 hub 据
# admin token 服务端派生:BFF **绝不传 X-Requested-By**(hub_client._headers 只注入 token)。


def _resolve_service(agent_id: str, service_code: str) -> Service:
    """按 (agentId=namespace.code, serviceCode) 反查台账 Service;任一缺失 → 404。

    两步走(复用 store.find_rows,不新增 join 查询):① namespace.code==agent_id 定位
    namespace_id;② Service(namespace_id, service_code)。返回 ORM 行(取 dir /
    nacos_service_name / default_image)。**404 文案统一**(不区分是 agent 还是 service 缺失,
    避免泄漏台账存在性)。
    """
    ns_rows = store.find_rows(Namespace, filters=[Namespace.code == agent_id], limit=1)
    if not ns_rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "节点服务不在台账")
    svc_rows = store.find_rows(
        Service,
        filters=[Service.namespace_id == ns_rows[0].id, Service.service_code == service_code],
        limit=1,
    )
    if not svc_rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "节点服务不在台账")
    return svc_rows[0]


def _derive_health_base_url(agent_id: str, nacos_service_name: str | None) -> str:
    """优雅 stop / redeploy 的 healthBaseUrl 派生(单 drain 近似)。

    `nacos_service_name` 缺失 → 400(优雅操作需配置 nacosServiceName);经 hub
    `list_instances` 取实例,无健康实例 → 409(请用 force);否则取**第一个**健康实例的
    address 拼 `http://<addr>`。

    ⚠️ 近似性说明(评审已知):compose `stop` 是**目录级**(停该 compose 项目下全部容器),
    而这里只 drain 第一个健康实例的 worker。多实例同目录场景下,其余实例不会被逐一 drain
    即随目录一起停 —— 这是 P1 可接受的近似(真正逐实例零中断走 restart+graceful 的 rolling
    路径)。`list_instances` 失败由调用方的 502 兜底捕获。
    """
    if not nacos_service_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "优雅操作需配置 nacosServiceName")
    r = hub_client.list_instances(agent_id, nacos_service_name)
    healthy = [i for i in (r.get("instances") or []) if isinstance(i, dict) and i.get("healthy")]
    if not healthy:
        raise HTTPException(status.HTTP_409_CONFLICT, "无健康实例可优雅 drain;请用 force")
    return f"http://{healthy[0]['address']}"


@router.post(
    "/{agent_id}/{service_code}/{action}",
    response_model=NodeActionOut,
    summary="节点操作下发",
    description="对某 (agent×service) 下发 启动/停止/重启/重部署;dir/nacosServiceName/image 由 Service 表权威派生;优雅 restart 走 hub 滚动重启,其余走 hub dispatch;requested_by 由 hub 派生。",
)
async def dispatch_node_action(
    body: NodeActionIn,
    agent_id: str = Path(title="Agent 标识(=namespace.code)"),
    service_code: str = Path(title="服务编码"),
    action: Literal["start", "stop", "restart", "redeploy"] = Path(title="操作(start/stop/restart/redeploy)"),
    _: str = Depends(require_session),
) -> NodeActionOut:
    # ① 台账寻址(404 早返,先于任何 hub 调用)。
    svc = _resolve_service(agent_id, service_code)
    dir_ = svc.dir
    nacos = svc.nacos_service_name
    default_image = svc.default_image
    mode = body.mode

    # ② mode 必填校验:stop / redeploy 须显式 mode;restart 缺省按 graceful;start 忽略 mode。
    if action in ("stop", "redeploy") and mode is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "stop/redeploy 须指定 mode")
    effective_mode = mode or "graceful"  # restart 缺省 graceful;start 下不参与决策

    # ③ action → hub 端点路由 + payload 组装。纯校验阶段(400/409/404)抛 HTTPException;
    #    真正的 hub 网络调用集中到下方 try 块,统一 HubError/httpx → 502 脱敏。
    try:
        if action == "restart" and effective_mode == "graceful":
            # 优雅 restart 复用 hub 零中断滚动重启(逐实例 drain);必须有 nacosServiceName。
            if not nacos:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "优雅 restart 需配置 nacosServiceName")
            resp = hub_client.rolling_restart(agent_id, nacos, force=False)
            return NodeActionOut(kind="rolling", task_id=resp["taskId"])

        # 其余路径走 dispatch:先把 payload 备齐(graceful stop/redeploy 需先派生 healthBaseUrl)。
        payload: dict[str, Any] = {"action": "", "dir": dir_}
        if action == "restart":  # force restart
            payload["action"] = "force-restart"
        elif action == "start":
            payload["action"] = "start"
        elif action == "stop" and effective_mode == "force":
            payload.update(action="stop", mode="force", serviceName=nacos, allowLastInstance=body.allow_last_instance)
        elif action == "stop":  # graceful stop
            health_base_url = _derive_health_base_url(agent_id, nacos)
            payload.update(action="stop", mode="graceful", healthBaseUrl=health_base_url, shutdownTimeoutSec=60, serviceName=nacos)
        elif action == "redeploy" and effective_mode == "force":
            if not default_image:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "重部署需配置 defaultImage")
            payload.update(action="pull-redeploy", mode="force", image=default_image)
        else:  # redeploy graceful
            if not default_image:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "重部署需配置 defaultImage")
            health_base_url = _derive_health_base_url(agent_id, nacos)
            payload.update(action="pull-redeploy", mode="graceful", image=default_image, healthBaseUrl=health_base_url)

        resp = hub_client.dispatch_command(agent_id, payload)
        # dispatch 成功:返回 hub 生成的 requestId(该命令进 /api/node-operations 审计列表)。
        # 取值放 try 内:hub 响应畸形(缺 command.requestId)同样收敛成脱敏 502,不逃逸成裸 500。
        return NodeActionOut(
            kind="command",
            request_id=resp["command"]["requestId"],
            accepted=resp.get("accepted", True),
        )
    except HTTPException:
        raise  # 业务校验(400/409/404)原样上抛,不被 502 吞掉
    except Exception as exc:  # noqa: BLE001
        # hub 调用失败(HubError / httpx 连接超时 / 畸形响应 / 任意异常)→ 502 脱敏:仅记异常
        # 类型名,绝不把内部消息(可能含 hub URL / token 上下文)回显给前端。
        logger.warning("节点操作下发失败 agent=%s service=%s action=%s: %s", agent_id, service_code, action, type(exc).__name__)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "下发命令失败") from exc


def _truncate_tail(value: str | None) -> str | None:
    """审计字段截尾:超 `_AUDIT_FIELD_MAX` 保留**末尾**字符 + 前缀标记(结果通常在尾部)。"""
    if value is None or len(value) <= _AUDIT_FIELD_MAX:
        return value
    return f"…(已截断)\n{value[-_AUDIT_FIELD_MAX:]}"


@operations_router.get(
    "/api/node-operations",
    response_model=NodeOperationsListOut,
    summary="操作审计列表",
    description="代理 hub /api/commands(start/stop/force-restart/redeploy 等 dispatch 命令)的操作审计;limit/offset 换算成平台 page/pageSize 信封。优雅 restart 走 rolling,本期不在此列表。",
)
async def list_node_operations(
    _: str = Depends(require_session),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
) -> NodeOperationsListOut:
    try:
        resp = hub_client.list_commands(page, page_size)
    except Exception as exc:  # noqa: BLE001 —— hub 任意失败(HubError/httpx/畸形)统一脱敏 502
        logger.warning("拉取操作审计失败 page=%s pageSize=%s: %s", page, page_size, type(exc).__name__)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "拉取操作审计失败") from exc

    items = resp.get("items") or []
    total = int(resp.get("total") or 0)
    rows = [
        NodeOperationOut(
            request_id=it.get("requestId"),
            agent_id=it.get("agentId"),
            action=it.get("action"),
            mode=it.get("mode"),
            status=it.get("status"),
            requested_by=it.get("requestedBy"),
            request_source=it.get("requestSource"),
            dir=it.get("dir"),
            image=it.get("image"),
            output=_truncate_tail(it.get("output")),
            error=_truncate_tail(it.get("error")),
            created_at=it.get("createdAt"),
            updated_at=it.get("updatedAt"),
        )
        for it in items
    ]
    return NodeOperationsListOut(
        count=total,
        rows=rows,
        page=page,
        page_size=page_size,
        total_page=math.ceil(total / page_size) if page_size else 0,
    )
