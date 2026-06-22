"""节点聚合路由(Task 9b)。

`GET /api/nodes`:节点页**行集仍以平台 Service 表为准**(每行 = (agent×service)),组合四处:
① Service 表静态(`serviceCode`/`nacosServiceName` + LEFT JOIN namespace.code → `agentId`/
`namespaceCode`);② hub 实时在线态(`hub_client.list_agents` 一次性拉全);③ 健康实例数
(`hub_client.list_instances` per-(agent,service) 并发 fan-out);④ **展示层 dir/defaultImage 的发现
权威覆盖(P3-6)**:若该 (agent, nacosService) 有**唯一** active DiscoveredNode,则 dir/defaultImage
优先显示其发现值(真值由 agent 发现而非手配),0 个回退 Service 值、多实例(>1)仍显示 Service 值
(列表 per-service 无法择一,per-instance 列表归后续实例页 SPA 批次)。批量取齐当页 agent 的发现行、
内存分组,避免逐行 N+1。
注:**节点操作下发的寻址权威**(dir/image/containerId)已整体迁到 DiscoveredNode,见下方
`_resolve_addressing`(本列表仅做展示层覆盖,完整 per-instance 列表后续 SPA 批次再做)。

约束 / 不变式:
- **响应全 camelCase**(评审 H2):`response_model=NodeListOut`,经 `*Out` 序列化,不手搓 dict。
- **纵深防御**:`Depends(require_session)`;`/api/` 前缀下 default-deny 中间件已先挡无/坏 JWT。
- **S5 进程内直调**:hub 调用全经 `hub_client`(已重写为进程内 async 适配器,直调 `app/hub/`
  路由 handler / hub_state,不再走 httpx + admin-token HTTP 跳)。这些函数现为 `async def`,调用点 `await`。
- **单 agent 卡死/超时不拖垮整页**(核心):每行 `list_instances` 用 `asyncio.wait_for(coro, timeout)`
  硬短超时 + `asyncio.gather(..., return_exceptions=True)` 收集异常/超时 → 该行 `degraded=True、
  healthyCount=None`,其余行照常、整页 200。进程内 await 无 socket 超时,wait_for 是唯一硬超时闸。
- **降级矩阵**:
  - online 且有 nacosServiceName → fan-out:成功(dict 且 status==success)→ healthyCount=健康数、
    degraded=False;异常 / status!=success → healthyCount=None、degraded=True。
  - 离线行(agent 不在 list_agents map 或其 online 为假)/ 无 nacosServiceName → 不 fan-out:
    healthyCount=None、degraded=False。
  - `list_agents` 整体失败 → map 退化为空 → 全部行按离线处理(不崩整页)。

打桩说明:hub 调用经**模块引用** `hub_client.list_agents(...)` / `hub_client.list_instances(...)`,
故测试 `monkeypatch.setattr(nodes.hub_client, "list_agents", ...)` 能生效(同 namespaces 的 H7 打桩)。
S5 后 `hub_client.*` 是 async,**替身须为 async**(返回 coroutine),否则 `await` 会 TypeError。
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app import hub_client, store
from app.auth import require_session
from app.db_models import Namespace, Service
from app.hub.db_models import DiscoveredNodeModel
from app.models import (
    DiscoveredNodeListOut,
    DiscoveredNodeOut,
    ManagedDownServiceOut,
    NodeActionIn,
    NodeActionOut,
    NodeListOut,
    NodeOperationOut,
    NodeOperationsListOut,
    NodeOut,
    ReconciliationOut,
    UnmanagedServiceOut,
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

# 优雅 stop / pull-redeploy 的 drain 超时(秒),由平台统一下发给 agent(经 hub 透传)。
# 两条优雅路径(stop / redeploy)共用同一常量,保证 drain 行为对称(评审 T10 Important)。
_GRACEFUL_SHUTDOWN_TIMEOUT_SEC = 60

# 节点列表 per-(agent×service) 健康实例 fan-out 的**硬短超时**(秒)。S5 进程内化后,
# 单 agent WS `call_agent` 不应答不再有 httpx socket 超时兜底,必须由本常量 + `asyncio.wait_for`
# 在 BFF 这层截断,否则单 agent 卡死会吊死整页 fan-out(核心不变式)。取 5s:足够正常 WS 往返,
# 又远小于会让节点页明显卡顿的阈值;与跨进程时代 hub_client.list_instances 的旧默认 5.0 对齐。
_FANOUT_TIMEOUT_SEC = 5.0

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


async def _load_agents_map() -> dict[str, dict[str, Any]]:
    """一次性拉 hub 在线态,转成 `{agentId: snapshot}`。

    `list_agents` 失败(配置缺失 / hub 逻辑失败 / 响应异常)→ **退化为空 map**,
    所有行据此按「离线/未知」处理,**绝不让整页 500**(核心不变式)。

    S5:`hub_client.list_agents` 改进程内 async,这里相应 `await`(失败退化为空 map 的不变式不变)。
    """
    try:
        agents = await hub_client.list_agents()
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
    agents_map = await _load_agents_map()

    # 标注每行在线态,并挑出「online 且有 nacosServiceName」的行做并发 fan-out。
    online_flags: list[bool] = []
    fanout_idx: list[int] = []  # 需 fan-out 的行在 rows 中的下标
    for i, row in enumerate(rows):
        agent = agents_map.get(row.get("namespace_code"))
        online = bool(agent and agent.get("online"))
        online_flags.append(online)
        if online and row.get("nacos_service_name"):
            fanout_idx.append(i)

    # ③ 并发 fan-out(S5:list_instances 改进程内 async)。每行用 `asyncio.wait_for(coro, timeout)`
    #    包一层**硬短超时** + `gather(return_exceptions=True)` 收集每行异常/超时:单 agent 的 WS
    #    `call_agent` 卡死 → 该行 wait_for 抛 TimeoutError(被 return_exceptions 收成异常对象)→
    #    `_healthy_count` 回 None → 该行 degraded,其余行 + 整页 200 不受影响(本任务核心不变式)。
    #    ⚠️ 必须 wait_for:进程内 await hub_state 不再有 httpx socket 超时,单 agent WS 不应答会
    #    无限挂起 gather → 吊死整页;wait_for 是这层唯一的硬超时闸。
    fanout_results: list[Any] = []
    if fanout_idx:
        fanout_results = await asyncio.gather(
            *[
                asyncio.wait_for(
                    hub_client.list_instances(
                        agents_map[rows[i]["namespace_code"]]["agentId"],
                        rows[i]["nacos_service_name"],
                    ),
                    timeout=_FANOUT_TIMEOUT_SEC,
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

    # ④ 展示层「发现权威」(P3-6,尽量):dir / defaultImage 若该 (agent, nacosService) 有**唯一**
    #    active DiscoveredNode,则优先显示其发现值(体现 dir/image 真值由 agent 发现而非手配);0 个 →
    #    显示 Service 值(回退);**多实例(>1)→ 仍显示 Service 值**(列表是 per-service,无法择一,
    #    per-instance 列表归后续实例页 SPA 批次)。一次性批量取齐当页所有 agent 的发现行,内存分组,
    #    避免逐行 N+1(列表已对每行做 list_instances fan-out,不再叠加 N 次 DB 往返)。
    page_agent_ids = {row.get("namespace_code") for row in rows if row.get("namespace_code")}
    discovered_rows = store.list_discovered_nodes_for_agents(page_agent_ids, status="active")
    # (agentId, nacosService) → 该组的发现行列表;仅 size==1 的组用于覆盖展示(唯一不歧义)。
    discovered_groups: dict[tuple[str, str], list[Any]] = {}
    for dn in discovered_rows:
        if dn.nacos_service:  # 无 nacosService 的发现行无法对回 Service 行,跳过
            discovered_groups.setdefault((dn.agent_id, dn.nacos_service), []).append(dn)

    out_rows: list[NodeOut] = []
    for i, row in enumerate(rows):
        agent = agents_map.get(row.get("namespace_code"))
        # 默认展示 Service 台账值;有唯一 active DiscoveredNode 时用发现值覆盖 dir/image。
        display_dir = row.get("dir")
        display_image = row.get("default_image")
        group = discovered_groups.get((row.get("namespace_code"), row.get("nacos_service_name")))
        if group and len(group) == 1:
            display_dir = group[0].dir
            display_image = group[0].image
        out_rows.append(
            NodeOut(
                agent_id=row.get("namespace_code") or "",
                service_code=row["service_code"],
                namespace_code=row.get("namespace_code"),
                dir=display_dir,
                default_image=display_image,
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


@router.get(
    "/instances",
    response_model=DiscoveredNodeListOut,
    summary="实例(发现节点)列表",
    description="分页返回 agent 自动发现上报的容器实例(DiscoveredNode);可按 namespace(=agentId)与 status(active/stale)过滤。dir/image/composeProject 为发现权威值;stale 行保留(失联可定位)。",
)
async def list_discovered_instances(
    _: str = Depends(require_session),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
    namespace: str | None = Query(default=None, title="按 namespace(=agentId)过滤"),
    status_filter: str | None = Query(default=None, alias="status", title="按状态过滤(active/stale)"),
) -> DiscoveredNodeListOut:
    # 实例页数据源 = DiscoveredNode(发现权威)。默认不按 status 过滤:stale 实例也要可见(已停但可 start,M8)。
    filters: list[Any] = []
    if namespace:
        filters.append(DiscoveredNodeModel.agent_id == namespace)
    if status_filter:
        filters.append(DiscoveredNodeModel.status == status_filter)
    rows, count = store.list_rows(DiscoveredNodeModel, page=page, page_size=page_size, filters=filters)
    return DiscoveredNodeListOut(
        count=count,
        rows=[DiscoveredNodeOut.model_validate(r) for r in rows],
        page=page,
        page_size=page_size,
        total_page=math.ceil(count / page_size) if page_size else 0,
    )


@router.get(
    "/reconciliation",
    response_model=ReconciliationOut,
    summary="服务对账(意图 ⋈ 现实)",
    description=(
        "实时计算(不落表)意图 Service ⋈ 现实 DiscoveredNode(按 nacosServiceName 关联)的三态:"
        "runningButUnmanaged(在跑但没纳管,『已发现未纳管』收件箱,同 nacosService 跨多 agent 聚合)/ "
        "managedButDown(纳管了但没实例)/ versionDrift(本期空)。数据量=服务数,不分页。"
        "**纳管动作不在此端点**:纳管 = 前端预填 namespace + nacosServiceName 后调既有 POST /api/services 创建 Service。"
    ),
)
async def reconcile_services(_: str = Depends(require_session)) -> ReconciliationOut:
    """服务对账:意图(`Service`)⋈ 现实(active `DiscoveredNode`),**按 `nacos_service_name` 关联**。

    判定逻辑(设计 §3.4):
    - **现实侧**:`store.aggregate_discovered_by_nacos(status="active")` → `{nacosService: [实例...]}`
      (nacosService 为 None 的容器已在 store 层跳过,不参与按服务对账)。
    - **意图侧**:`store.list_services_with_namespace_code()` → 全部 Service 行(含 namespaceCode)。
    - **runningButUnmanaged**:现实侧出现、但其 nacosService **∉** 任何 `Service.nacos_service_name`
      的服务 → 「已发现未纳管」收件箱。同一 nacosService 跨多 agent 的实例聚成一项(评审 H-5):
      `agentIds` 汇总承载该服务的全部 agent(去重、稳定序),`instanceCount` 为跨 agent 实例合计。
    - **managedButDown**:`Service.nacos_service_name` **非空**、但该 nacos 名**无任何 active 发现实例**
      的 Service → 「该起没起」。(nacosServiceName 为空的 Service 无法按 nacos 对账,直接跳过——
      它既不算 down 也无从关联实例。)
    - **versionDrift**:**本期恒空** + TODO(需实例携带插件版本,DiscoveredNode 暂无该字段;镜像漂移
      另见设计 P4-4)。不硬凑。
    """
    # 现实侧:按 nacos 名聚合的 active 发现实例(跨 agent)。
    discovered_by_nacos = store.aggregate_discovered_by_nacos(status="active")
    # 意图侧:全部 Service(含 namespaceCode);收集「已纳管的 nacos 名集合」用于判收件箱。
    services = store.list_services_with_namespace_code()
    managed_nacos_names = {
        svc["nacos_service_name"] for svc in services if svc["nacos_service_name"]
    }

    # ① runningButUnmanaged:发现实例的 nacosService ∉ 已纳管集合 → 收件箱(跨 agent 聚合)。
    running_but_unmanaged: list[UnmanagedServiceOut] = []
    for nacos_name, instances in discovered_by_nacos.items():
        if nacos_name in managed_nacos_names:
            continue
        # 跨多 agent 聚合 agentIds(去重 + 稳定序:按首次出现顺序);instanceCount = 跨 agent 实例合计。
        agent_ids: list[str] = []
        for dn in instances:
            if dn.agent_id not in agent_ids:
                agent_ids.append(dn.agent_id)
        running_but_unmanaged.append(
            UnmanagedServiceOut(
                nacos_service=nacos_name,
                agent_ids=agent_ids,
                instance_count=len(instances),
            )
        )

    # ② managedButDown:Service.nacos_service_name 非空但无 active 发现实例 → 该起没起。
    managed_but_down: list[ManagedDownServiceOut] = []
    for svc in services:
        nacos_name = svc["nacos_service_name"]
        if not nacos_name:  # 无 nacos 名的 Service 无法按 nacos 对账,跳过(既非 down 也无从关联)
            continue
        if nacos_name not in discovered_by_nacos:
            managed_but_down.append(
                ManagedDownServiceOut(
                    service_code=svc["service_code"],
                    nacos_service_name=nacos_name,
                    namespace_code=svc["namespace_code"],
                )
            )

    # ③ versionDrift:本期恒空(DiscoveredNode 暂无实例插件版本字段,无从比对;镜像漂移见 P4-4)。
    # TODO(P4-4 / 实例上报插件版本后):实例携带各插件版本 → 与 active service_plugin_version 比对,
    #   不一致的服务进 versionDrift(待投放)。需先给 DiscoveredNode/上报协议加插件版本字段。
    return ReconciliationOut(
        running_but_unmanaged=running_but_unmanaged,
        managed_but_down=managed_but_down,
        version_drift=[],
    )


# =====================================================================================
# Task 10b:节点操作下发 + 操作审计  /  P3-6:寻址权威迁到 DiscoveredNode(发现权威)
# =====================================================================================
#
# 寻址的**权威源 = agent 自动发现上报的 DiscoveredNode**(`discovered_nodes`,承载 dir / image /
# containerId / composeProject 的真值),**不是**手配的 `Service.dir/default_image`。Service 表只
# 承载「逻辑服务 + nacosServiceName 入口」,其 dir/default_image **退化为迁移期回退**——仅当某
# (agent, nacosService) 暂无 DiscoveredNode 时才用。解析两步:① _resolve_service 拿 Service 行
# (404 早返 + nacosServiceName 入口 + 回退值);② 按 (agentId, nacosServiceName) 查 DiscoveredNode
# (active),按 0 / 1 / 多 分支定位(见 _resolve_addressing)。
#
# **BFF 仍绝不接受客户端传路径或任意 image**——dir/image 一律由 DiscoveredNode(或回退 Service)派生
# (防越权操作非授权目录 / 拉非白名单镜像)。requested_by 由 hub 据 admin token 服务端派生:BFF
# **绝不自报 requested_by**(S5 进程内化后,hub_client.dispatch_command 固定传 requested_by_hint=None,
# handler 内 _derive_requested_by 据 token 得 platform-admin)。


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


@dataclass(frozen=True)
class _NodeAddressing:
    """寻址解析结果(P3-6「发现权威」):节点操作据此派生 hub payload 的 dir / image / 工程对齐。

    - `dir` / `image`:**优先取自 DiscoveredNode(权威)**,仅 (agent, nacosService) 暂无
      DiscoveredNode 时回退 `Service.dir` / `Service.default_image`。
    - `container_id`:DiscoveredNode 的真实容器 id(回退路径下为 None;当前 dispatch payload 暂不
      消费它,留作将来逐实例精确操作)。
    - `nacos_service_name`:= Service.nacos_service_name(逻辑服务入口,优雅路径校验/透传用)。
    - `expected_compose_project`:优先 DiscoveredNode 的真实 `compose_project`(agent 发现的真值,
      比从 dir basename 猜更准);DiscoveredNode 无此值 / 无 DiscoveredNode 时回退
      `_compose_default_project(dir)` 启发式。供优雅 drain 做工程对齐守卫(matched)。
    """

    dir: str | None
    image: str | None
    container_id: str | None
    nacos_service_name: str | None
    expected_compose_project: str | None


def _resolve_addressing(
    agent_id: str, service_code: str, compose_project: str | None = None
) -> _NodeAddressing:
    """寻址解析(P3-6 核心):入口取 Service 行,权威值取自 agent 发现的 DiscoveredNode。

    步骤:
    ① `_resolve_service(agent_id, service_code)` 拿 Service 行(404 早返;提供 nacosServiceName 入口
       与迁移期回退 dir/default_image)。
    ② 按 `(agent_id, Service.nacos_service_name)` 查 DiscoveredNode(active):
       - **0 个**(或 Service 无 nacosServiceName,无法按 nacos 收敛实例)→ 回退:dir/image 取
         Service.dir/default_image,containerId 无,expectedComposeProject 走 dir 启发式。
       - **1 个** → **权威**:dir/image/containerId 取自该 DiscoveredNode;expectedComposeProject 取
         其真实 compose_project(为空再回退 dir 启发式)。
       - **>1 个(多实例:同一 nacosService 多 compose 工程,如 admin / 2admin)** → 必须用
         `compose_project` 指定其一:命中则按该实例定位;未带 compose_project → **409**;带了但无任何
         实例匹配 → **404**(指定实例不存在)。

    dir/image 一律**优先 DiscoveredNode,Service 仅回退**(发现权威,不可搞反)。
    """
    svc = _resolve_service(agent_id, service_code)
    nacos = svc.nacos_service_name

    # Service 无 nacosServiceName:无法按 nacos 把发现实例收敛到本逻辑服务 → 直接回退 Service 台账值
    # (force 等只依赖 dir 的操作仍可用;优雅路径会因缺 nacos 在调用方早返 400)。
    if not nacos:
        return _NodeAddressing(
            dir=svc.dir,
            image=svc.default_image,
            container_id=None,
            nacos_service_name=nacos,
            expected_compose_project=_compose_default_project(svc.dir),
        )

    discovered = store.list_discovered_nodes(agent_id, nacos, status="active")

    if not discovered:
        # 迁移期回退:该 (agent, nacosService) 尚无发现上报(agent 未升级 / 未上报)→ 用手配 Service 值。
        return _NodeAddressing(
            dir=svc.dir,
            image=svc.default_image,
            container_id=None,
            nacos_service_name=nacos,
            expected_compose_project=_compose_default_project(svc.dir),
        )

    if len(discovered) == 1:
        dn = discovered[0]
    else:
        # 多实例:必须由 composeProject 指定操作哪一个 compose 工程的实例。
        if not compose_project:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "该服务有多个实例(compose 工程),请指定 composeProject",
            )
        matched = [d for d in discovered if d.compose_project == compose_project]
        if not matched:
            # 指定了 composeProject 却无对应实例:目标实例不存在(404,语义同台账缺失,不暴露实例集)。
            raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的 composeProject 无对应实例")
        dn = matched[0]

    # 命中单个 DiscoveredNode(1 个或多实例选中):dir/image/containerId 取发现权威值;
    # expectedComposeProject 优先用发现的真实 compose_project,为空再回退 dir 启发式。
    return _NodeAddressing(
        dir=dn.dir,
        image=dn.image,
        container_id=dn.container_id,
        nacos_service_name=nacos,
        expected_compose_project=dn.compose_project or _compose_default_project(dn.dir),
    )


def _compose_default_project(dir_: str | None) -> str | None:
    """从 compose 目录推 Docker **默认**工程名:目录 basename → 小写 → 仅留 [a-z0-9_-] → 去前导非字母数字。

    Docker Compose 在未设 `COMPOSE_PROJECT_NAME` / `-p` 时,默认工程名取自工作目录 basename,
    并做规范化(转小写、剔除非 `[a-z0-9_-]`、去掉开头的非 `[a-z0-9]` 字符)。本函数复刻该启发式,
    供优雅 drain 把它作为 `expectedComposeProject` 传给 agent 做工程对齐(评审 #11)。

    ⚠️ 启发式取舍(评审 #11 注明):仅在部署**沿用 compose 默认工程名**时准确。若部署用
    `COMPOSE_PROJECT_NAME` 或 `-p` 覆盖了工程名,这里推出来的值会与容器实际 compose project 不符,
    导致 agent 守卫把本机容器也置 `matched=False` → 优雅 drain 因「无本机 matched 实例」回 409。
    这是 **fail-safe(宁拒不误 drain)**:拒绝总比 drain 错节点安全,运维改用 force 即可。`dir_`
    为空时返回 None(不带 expectedComposeProject,优雅路径随后会因缺 nacos 等早返)。
    """
    if not dir_:
        return None
    # 兼容 Windows / POSIX 分隔符,去尾部斜杠后取最后一段(basename)。
    base = dir_.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    # 仅保留 [a-z0-9_-](已小写;限 ASCII 字母数字,避免非 ASCII 字母被 isalnum 放过)。
    kept = "".join(ch for ch in base if (ch.isascii() and ch.isalnum()) or ch in "_-")
    # 去掉前导非字母数字(Docker 默认工程名规范化要求开头必须是 [a-z0-9])。
    start = 0
    while start < len(kept) and not (kept[start].isascii() and kept[start].isalnum()):
        start += 1
    return kept[start:] or None


async def _derive_health_base_url(
    agent_id: str, nacos_service_name: str | None, expected_compose_project: str | None
) -> str:
    """优雅 stop / redeploy 的 healthBaseUrl 派生(只选**本机** matched 健康实例)。

    `nacos_service_name` 缺失 → 400(优雅操作需配置 nacosServiceName);经 hub
    `list_instances`(带寻址解析出的 `expected_compose_project` —— P3-6 后优先来自 DiscoveredNode 的
    真实 compose_project,无则回退 dir basename 启发式)取实例,**过滤 healthy 且 matched** 后无实例
    → 409(请用 force);否则取过滤后**第一个**实例的 address 拼 `http://<addr>`。

    ⚠️ 为什么必须 matched(评审 #3/multi-node):nacos 是**集群级**注册中心,`healthy` 实例可能在
    **别的节点**;agent 恒置 `healthy=True`,只有 `matched` 反映「该容器在**本机**找到(端口/IP 匹配,
    且 compose 工程对齐)」。若不看 matched 而取第 0 个 healthy,可能 drain 了别节点的 worker,再
    compose stop 本节点目录 → 本节点没排空就停、别节点被无故 drain。故这里只选本机 matched 实例;
    全无本机 matched 实例时拒绝(409),让调用方改用 force(force 是目录级,不依赖按实例 drain)。

    ⚠️ 单 drain 近似(评审已知):compose `stop` 是**目录级**(停该 compose 项目下全部容器),
    而这里只 drain 第一个本机健康实例的 worker。多本机实例同目录场景下,其余实例随目录一起停,不逐一
    drain —— P1 可接受的近似(真正逐实例零中断走 restart+graceful 的 rolling 路径)。`list_instances`
    失败由调用方的 502 兜底捕获。
    """
    if not nacos_service_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "优雅操作需配置 nacosServiceName")
    # expected_compose_project 由寻址解析给出(P3-6:优先 DiscoveredNode 真实工程名,无则 dir 启发式),
    # 作为 expected 传下去:matched 由此同时校验「本机 + 同 compose 工程」。
    r = await hub_client.list_instances(
        agent_id, nacos_service_name, expected_compose_project=expected_compose_project
    )
    # 评审 #3:只选 healthy **且** matched(本机)的实例;别节点(matched=False)的 healthy 实例一律排除。
    local_healthy = [
        i
        for i in (r.get("instances") or [])
        if isinstance(i, dict) and i.get("healthy") and i.get("matched")
    ]
    if not local_healthy:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "本节点无可 drain 的本机健康实例(优雅操作需本机实例,或改用 force)"
        )
    return f"http://{local_healthy[0]['address']}"


@router.post(
    "/{agent_id}/{service_code}/{action}",
    response_model=NodeActionOut,
    summary="节点操作下发",
    description="对某 (agent×service) 下发 启动/停止/重启/重部署;dir/image 优先取 agent 发现的 DiscoveredNode(权威),仅无发现时回退 Service 台账;多实例须传 composeProject;优雅 restart 走 hub 滚动重启,其余走 hub dispatch;requested_by 由 hub 派生。",
)
async def dispatch_node_action(
    body: NodeActionIn,
    agent_id: str = Path(title="Agent 标识(=namespace.code)"),
    service_code: str = Path(title="服务编码"),
    action: Literal["start", "stop", "restart", "redeploy"] = Path(title="操作(start/stop/restart/redeploy)"),
    _: str = Depends(require_session),
) -> NodeActionOut:
    # ① 寻址解析(404 早返 + 多实例 409,先于任何 hub 调用)。P3-6「发现权威」:dir/image 优先取
    #    agent 发现的 DiscoveredNode,仅该 (agent, nacosService) 无发现时回退手配 Service.dir/default_image;
    #    同一 nacosService 多实例(多 compose 工程)须由 body.composeProject 指定其一(否则 409)。
    addr = _resolve_addressing(agent_id, service_code, body.compose_project)
    dir_ = addr.dir
    nacos = addr.nacos_service_name
    default_image = addr.image
    mode = body.mode

    # ② mode 必填校验:stop / redeploy 须显式 mode;restart 缺省按 graceful;start 忽略 mode。
    if action in ("stop", "redeploy") and mode is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "stop/redeploy 须指定 mode")
    effective_mode = mode or "graceful"  # restart 缺省 graceful;start 下不参与决策

    # ③ action → hub 端点路由 + payload 组装。纯校验阶段(400/409/404)抛 HTTPException;
    #    真正的 hub 调用(S5 后为进程内 await)集中到下方 try 块,非 HTTPException 失败统一 → 502 脱敏。
    try:
        if action == "restart" and effective_mode == "graceful":
            # 优雅 restart 复用 hub 零中断滚动重启(逐实例 drain);必须有 nacosServiceName。
            if not nacos:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "优雅 restart 需配置 nacosServiceName")
            resp = await hub_client.rolling_restart(agent_id, nacos, force=False)
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
            health_base_url = await _derive_health_base_url(agent_id, nacos, addr.expected_compose_project)
            payload.update(action="stop", mode="graceful", healthBaseUrl=health_base_url, shutdownTimeoutSec=_GRACEFUL_SHUTDOWN_TIMEOUT_SEC, serviceName=nacos)
        elif action == "redeploy" and effective_mode == "force":
            if not default_image:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "重部署需配置 defaultImage")
            payload.update(action="pull-redeploy", mode="force", image=default_image)
        else:  # redeploy graceful
            if not default_image:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "重部署需配置 defaultImage")
            health_base_url = await _derive_health_base_url(agent_id, nacos, addr.expected_compose_project)
            payload.update(action="pull-redeploy", mode="graceful", image=default_image, healthBaseUrl=health_base_url, shutdownTimeoutSec=_GRACEFUL_SHUTDOWN_TIMEOUT_SEC)

        resp = await hub_client.dispatch_command(agent_id, payload)
        # dispatch 成功:返回 hub 生成的 requestId(该命令进 /api/node-operations 审计列表)。
        # 取值放 try 内:hub 响应畸形(缺 command.requestId)同样收敛成脱敏 502,不逃逸成裸 500。
        return NodeActionOut(
            kind="command",
            request_id=resp["command"]["requestId"],
            accepted=resp.get("accepted", True),
        )
    except HTTPException:
        # 原样上抛,不被 502 吞掉。涵盖两类:① BFF 自身的业务校验(400/409/404,如台账缺失、
        #   优雅 drain 无本机实例);② S5 进程内化后 hub handler 直接抛的 HTTPException(agent
        #   不存在 404 / 离线 409 / force-stop 护栏 400)—— 跨进程时代这些被 httpx 包成网络错落进
        #   下方 502,进程内后语义更准(直接透出 hub 的精确状态码),刻意保留。
        raise
    except Exception as exc:  # noqa: BLE001
        # hub 调用其余失败(HubError / 畸形响应 / 任意非 HTTPException 异常)→ 502 脱敏:仅记异常
        # 类型名,绝不把内部消息回显给前端。
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
        resp = await hub_client.list_commands(page, page_size)
    except Exception as exc:  # noqa: BLE001 —— hub 任意失败(HubError/畸形)统一脱敏 502
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
