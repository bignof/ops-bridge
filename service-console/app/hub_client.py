"""hub 控制链的**进程内适配器**(S5:删 platform→hub 的跨进程 HTTP 跳)。

历史:hub 原是独立 service-hub 进程,平台经 `httpx` + `X-Admin-Token` 调它的 REST 端点
(provision / rotate / list-agents / list-instances / dispatch / rolling / list-commands)。
S1–S4 已把 hub 并入 console 同进程(`app/hub/`,与 console 共用单一 DB / 单例 `hub_state`),
那条 HTTP 跳变成「同进程自己调自己」的多余网络往返。本模块据此重写为**进程内直调**:

- **保留 7 个函数名 + 调用契约**(返回的 camelCase 形状与 `nodes.py` / `namespaces.py`
  现有消费一致),但函数体改为 `await` 调 `app/hub/` 的路由 handler / `_build_command_list_response`,
  不再走 httpx。函数全部改 **async def**(hub_state 方法是 async);调用点相应 `await`。
- **复用 hub 路由 handler 的编排 + 序列化**:dispatch / rolling / list-instances 的 WS 调用、
  force 护栏、命令落库、滚动后台任务等编排逻辑全部复用 handler,不在此重搓;响应用 handler 返回的
  Pydantic 模型 `.model_dump(by_alias=True)` 转 camelCase dict,杜绝手搓 dict 形状漂移。
- **requested_by 进程内服务端派生**:dispatch 经 handler 内 `_derive_requested_by(admin_token)`
  得固定审计身份 `platform-admin`(单 admin 模型),**不信客户端自报**(BFF 不传 X-Requested-By)。
- **admin_token**:进程内调 handler 时显式传 `settings.admin_token`,满足 handler 首行
  `_require_admin_token` 自校验(与 hub 对外 HTTP admin-token 入口同一道闸)。生产环境
  `ADMIN_TOKEN` 为 hub 控制链硬依赖(本就必配),故此处不引入新约束。
- **删配置**:不再读 `SERVICE_HUB_URL` / `HUB_ADMIN_TOKEN`(已从 `app/config.py` 移除),
  也不再 import httpx。

错误契约(供 `namespaces.py` 复用):provision / rotate 失败(handler 抛 HTTPException、或响应缺
agentKey)统一收敛为 `HubError`,由路由层映射 502 脱敏 + 补偿删除孤儿行。读 / dispatch / rolling /
list-commands 的失败语义沿用各 handler(list-instances 失败抛 HTTPException(502),由 `nodes.py`
fan-out 的 `return_exceptions=True` 收成该行 degraded)。

> 注:hub 自身的对外 HTTP 路由(`/api/agents` 等)**保留不动**——它们仍是外部 admin-token 入口;
> 本模块只把 platform 内部从「HTTP 调自己」改成「进程内调」。
"""

from __future__ import annotations

from typing import Any


class HubError(Exception):
    """hub 进程内调用失败(provision / rotate 未拿到 agentKey 等)。

    保留此异常名是为兼容 `namespaces.py` 既有的 `except hub_client.HubError` 错误映射
    (→ 502 脱敏 + 补偿删除)。跨进程时代它表示「连不上 / hub 业务性失败」,进程内后它表示
    「hub 逻辑层失败」,对调用方语义不变。
    """


def _platform_admin_token() -> str:
    """取本进程 admin token,用于满足 hub handler 首行 `_require_admin_token` 自校验。

    延迟 import settings(避免模块级循环 import),每次读现值(测试可 object.__setattr__ 覆盖)。
    """
    from app.config import settings

    return settings.admin_token


async def provision_agent(agent_id: str) -> str:
    """进程内创建 Agent(命名空间)并返回首次连接密钥 agentKey。

    直调 hub 的 `provision_agent` 路由 handler(复用其 409-already-exists 判定与 *Response 序列化),
    再从响应模型取 `agent_key`。任何失败(handler 抛 HTTPException、响应缺 key)归一化为 `HubError`,
    使 `namespaces.py` 的 502 映射 + 补偿删除照常生效。
    """
    from app.hub.routers.agents import provision_agent as _provision_handler

    try:
        resp = await _provision_handler(
            AgentProvisionRequest(agentId=agent_id),
            admin_token=_platform_admin_token(),
        )
    except Exception as exc:  # noqa: BLE001 —— handler 任意失败(含 HTTPException)统一归一化为 HubError
        raise HubError("hub provision 失败") from exc
    key = getattr(resp, "agent_key", None)
    if not key:
        raise HubError("hub 未返回 agentKey")
    return key


async def rotate_agent_key(agent_id: str) -> str:
    """进程内轮换指定 Agent 的连接密钥,返回新 agentKey(旧密钥立即失效)。

    直调 hub 的 `rotate_agent_credentials` 路由 handler(复用其 *Response 序列化),取 `agent_key`。
    失败归一化为 `HubError`(契约同 provision)。
    """
    from app.hub.routers.agents import rotate_agent_credentials as _rotate_handler

    try:
        resp = await _rotate_handler(agent_id=agent_id, admin_token=_platform_admin_token())
    except Exception as exc:  # noqa: BLE001
        raise HubError("hub rotate 失败") from exc
    key = getattr(resp, "agent_key", None)
    if not key:
        raise HubError("hub 未返回 agentKey")
    return key


async def list_agents() -> list[dict]:
    """进程内拉取全部 Agent 状态快照(供节点页判在线态),返回 camelCase dict 列表。

    直调 hub 的 `list_agents` 路由 handler,把每个 `AgentSnapshot` 序列化成 camelCase dict
    (`agentId` / `online` / `lastSeenAt` 等),形状与 `nodes._load_agents_map` 现有消费一致。
    失败向上抛,由 `nodes._load_agents_map` 退化为空 map(不阻塞整页)。
    """
    from app.hub.routers.agents import list_agents as _list_agents_handler

    snapshots = await _list_agents_handler(admin_token=_platform_admin_token())
    return [s.model_dump(by_alias=True) for s in snapshots]


async def list_instances(
    agent_id: str,
    service_name: str,
    expected_compose_project: str | None = None,
    timeout: float = 5.0,
) -> dict:
    """进程内经指定 Agent 查询某 service 的容器实例(含健康状态)。

    直调 hub 的 `list_agent_instances` 路由 handler(复用其 agent 在线判定、WS `call_agent`、
    超时 / 断连 → 502 脱敏、status 校验等全部编排),返回 `{status, instances:[...]}` 形状
    (instances 各项含 `address` / `healthy` / `matched` / `composeProject` 等,直透 agent 上报)。

    `timeout` 形参为兼容旧签名而保留:跨进程时代它是 httpx 超时,进程内后真正的短超时由
    handler 内部 `list_instances_timeout`(对 WS `call_agent` 的 `asyncio.wait_for`)负责;
    BFF 侧的硬超时由 `nodes.list_nodes` 的 fan-out 用 `asyncio.wait_for(coro, timeout)` 兜底
    (单 agent WS 卡死不拖垮整页 —— 本任务核心不变式)。`expected_compose_project` 非空时随
    body 传 `expectedComposeProject`(触发 agent 的 compose 工程漂移守卫)。

    失败(agent 离线 / 未应答 / status!=success)由 handler 抛 `HTTPException`;`nodes.py` 的
    `gather(return_exceptions=True)` 把它(及 wait_for 超时)收成该行 degraded。
    """
    from app.hub.routers.commands import list_agent_instances as _list_instances_handler

    req = ListInstancesRequest(
        serviceName=service_name,
        expectedComposeProject=expected_compose_project or None,
    )
    resp = await _list_instances_handler(req, agent_id=agent_id, admin_token=_platform_admin_token())
    return resp.model_dump(by_alias=True)


async def dispatch_command(agent_id: str, payload: dict, timeout: float = 15.0) -> dict:
    """进程内向指定 Agent 下发一条命令(start/stop/force-restart/pull-redeploy 等),返回 202 响应。

    直调 hub 的 `dispatch_command` 路由 handler(复用其 agent 在线判定、force stop 护栏、命令落库、
    WS 下发、失败 → 502 等全部编排),返回 `{accepted, command:{requestId, ...}}` camelCase dict。

    **requested_by 进程内服务端派生**:handler 内 `_derive_requested_by(admin_token)` 得固定身份
    `platform-admin`;BFF 不传 X-Requested-By(`requested_by_hint=None`),审计 caller 不可伪造。
    `timeout` 形参兼容旧签名(进程内无 httpx 超时,故不使用)。`payload` 形状沿用旧契约(camelCase
    键:`action` / `dir` / 可选 `mode` / `image` / `serviceName` / `allowLastInstance` /
    `healthBaseUrl` / `shutdownTimeoutSec`),经 `CommandDispatchRequest(**payload)` 校验后传 handler。
    失败由 handler 抛 `HTTPException`,沿用 `nodes.dispatch_node_action` 的 502 脱敏兜底。
    """
    from app.hub.routers.commands import dispatch_command as _dispatch_handler

    request = CommandDispatchRequest(**payload)
    resp = await _dispatch_handler(
        request,
        agent_id=agent_id,
        admin_token=_platform_admin_token(),
        requested_by_hint=None,  # 安全:BFF 绝不自报 requested_by,由 handler 据 admin token 派生
        request_source="console",
    )
    return resp.model_dump(by_alias=True)


async def rolling_restart(agent_id: str, service_name: str, force: bool = False, timeout: float = 15.0) -> dict:
    """进程内触发某 (agent×service) 的零中断滚动重启,返回 `{taskId}`。

    直调 hub 的 `rolling_restart` 路由 handler(复用其 RollingConflict→409、滚动后台任务创建等编排)。
    handler 返回的就是 `{"taskId": ...}` 普通 dict(非 Pydantic),原样回。`timeout` 形参兼容旧签名。
    失败由 handler 抛 `HTTPException`,沿用 `nodes.dispatch_node_action` 的 502 脱敏兜底。
    """
    from app.hub.routers.rolling import RollingRestartRequest
    from app.hub.routers.rolling import rolling_restart as _rolling_handler

    request = RollingRestartRequest(agentId=agent_id, serviceName=service_name, force=force)
    return await _rolling_handler(request, admin_token=_platform_admin_token())


async def list_commands(page: int, page_size: int, timeout: float = 15.0) -> dict:
    """进程内拉取全局命令历史(操作审计),返回 hub `CommandListResponse` camelCase dict。

    **分页换算**:平台对外统一 page/pageSize 信封,hub 内部是 limit/offset;此处转换
    `limit=page_size, offset=(page-1)*page_size`,再调 `_build_command_list_response`(api_support
    里的真正实现,复用其过滤 / 排序 / 序列化),把返回的 `CommandListResponse` 序列化成 camelCase dict
    (`{items:[...], total, limit, offset, hasMore, sortBy, order}`),形状与 `nodes.list_node_operations`
    现有消费一致。`timeout` 形参兼容旧签名。失败向上抛,沿用 `nodes` 侧 502 脱敏兜底。
    """
    from app.hub.api_support import _build_command_list_response

    page = max(1, page)
    page_size = max(1, page_size)
    resp = await _build_command_list_response(
        agent_id=None,
        status_filter=None,
        action=None,
        requested_by=None,
        request_source=None,
        created_after=None,
        created_before=None,
        sort_by="createdAt",
        order="desc",
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return resp.model_dump(by_alias=True)


# 模型在函数体内构造(provision/rotate/list-instances/dispatch 入参),集中在文件尾部 import
# 以避免与 `app.hub.routers.*` 的函数级延迟 import 风格混淆;这些 model 无重依赖,模块级 import 安全。
from app.hub.models import (  # noqa: E402
    AgentProvisionRequest,
    CommandDispatchRequest,
    ListInstancesRequest,
)
