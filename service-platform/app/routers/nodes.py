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
import math
from typing import Any

from fastapi import APIRouter, Depends, Query

from app import hub_client, store
from app.auth import require_session
from app.db_models import Namespace, Service
from app.models import NodeListOut, NodeOut


router = APIRouter(prefix="/api/nodes", tags=["节点控制"])

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
    except Exception:  # noqa: BLE001 —— hub 任意失败都不得拖垮整页,统一退化为空 map
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
