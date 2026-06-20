"""service-hub 命名空间(Agent)管理客户端:provision / rotate。

平台是 service-hub 的管理面。本模块以 **admin token** 调用 hub 的 Agent 端点:
- `provision_agent` → POST `{hub}/api/agents`,body `{agentId}`,返回 `agentKey`(首次连接密钥)。
- `rotate_agent_key` → POST `{hub}/api/agents/{id}/credentials/rotate`,返回新 `agentKey`(旧密钥立即失效)。

端点形状以 `service-hub/app/routers/agents.py` 为准:请求 body 用 camelCase `agentId`
(`AgentProvisionRequest`),响应经 `titled_model_config`(`alias_generator=to_camel` +
`serialize_by_alias=True`)序列化,故密钥字段在网络上是 `agentKey`(非 `agent_key`)。

绑定约束:
- `HUB_ADMIN_TOKEN` 仅服务端持有,调 hub 时放进 `X-Admin-Token` header(对应 hub 侧
  `_require_admin_token`)。
- **敏感串严禁记日志**:本模块不打印 / log token 与 agentKey;异常消息也不携带它们。
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from app.config import settings


# 调 hub 的超时(秒)。provision/rotate 是轻量写操作,15s 足够,且避免无限挂起拖垮调用方。
_HUB_TIMEOUT = 15


class HubError(Exception):
    """调用 service-hub 失败(未配置 hub、hub 未返回 agentKey 等)。"""


def _headers() -> dict[str, str]:
    """携带 admin token 的请求头;token 仅服务端持有,不落日志。"""
    return {"Content-Type": "application/json", "X-Admin-Token": settings.hub_admin_token}


def _extract_agent_key(r: httpx.Response) -> str:
    """从已 raise_for_status(2xx)的响应里取 agentKey,**任何异常态归一化为 HubError**。

    复审 R3:hub 返 200 但 body 非 JSON(反代 HTML 错误页 → `r.json()` 抛 `ValueError`/
    `JSONDecodeError`)或 body 是 JSON 数组/标量(`.get` 抛 `AttributeError`)时,原实现会让这些
    异常逃出路由层「只捕 HubError/httpx.HTTPError」的窄 except → 裸 500 + 孤儿 namespace(补偿
    删除不执行)。这里把「非 JSON / 非 dict / 缺 agentKey」三种异常态统一收敛成 HubError,使路由层
    的 A13(502)映射 + A14(补偿删除)正常生效。敏感串不入异常消息(对齐本模块约定)。
    """
    try:
        data = r.json()
    except ValueError as exc:  # JSONDecodeError ⊂ ValueError:body 非 JSON
        raise HubError("hub 响应非 JSON") from exc
    if not isinstance(data, dict):  # JSON 数组/标量:.get 会抛 AttributeError
        raise HubError("hub 响应不是 JSON 对象")
    key = data.get("agentKey")
    if not key:
        raise HubError("hub 未返回 agentKey")
    return key


def provision_agent(agent_id: str) -> str:
    """创建 Agent(命名空间)并返回其首次连接密钥 agentKey。

    SERVICE_HUB_URL 未配置时直接抛 `HubError`(不发起请求),把配置缺失从「连不上的
    网络错误」前移成明确的配置错误。
    """
    if not settings.service_hub_url:
        raise HubError("SERVICE_HUB_URL 未配置")
    r = httpx.post(
        f"{settings.service_hub_url}/api/agents",
        headers=_headers(),
        json={"agentId": agent_id},
        timeout=_HUB_TIMEOUT,
    )
    r.raise_for_status()
    return _extract_agent_key(r)  # 复审 R3:非 JSON / 非 dict / 缺 key 统一归一化为 HubError


def list_agents() -> list[dict]:
    """拉取 hub 当前所有 Agent 的状态快照(供节点页判在线态)。

    `GET {hub}/api/agents`,返回 hub `AgentSnapshot` 列表(camelCase:`agentId` /
    `online` / `lastSeenAt` 等,见 `service-hub/app/models.py` 的 `titled_model_config`
    序列化别名)。SERVICE_HUB_URL 未配置 → 直接抛 `HubError`(把配置缺失从「连不上的
    网络错误」前移成明确的配置错误,与 provision/rotate 一致)。

    失败(配置缺失 / 连接 / 超时 / 非 2xx)向上抛;由节点路由层 catch → map 退化为空、
    全部行按离线处理,**不阻塞整页**。本函数不打印 / log token(敏感串约定)。
    """
    if not settings.service_hub_url:
        raise HubError("SERVICE_HUB_URL 未配置")
    r = httpx.get(
        f"{settings.service_hub_url}/api/agents",
        headers=_headers(),
        timeout=_HUB_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def list_instances(agent_id: str, service_name: str, timeout: float = 5.0) -> dict:
    """经指定 Agent 查询某 service 当前容器实例(含健康状态),供节点页算健康实例数。

    `POST {hub}/api/agents/{agentId}/list-instances`,body `{serviceName}`,返回
    hub `ListInstancesResponse`(`{status, instances}`;instances 各项含 `address` /
    `containerId` / `healthy` / `matched` / `composeProject`,见 T9a `commands.py`)。

    `timeout` 故意**短**(默认 5s):节点页对每行并发 fan-out 调本函数,单 agent/nacos 卡死
    必须被这层短超时截断,配合路由层 `gather(return_exceptions=True)` 把该行标 degraded,
    保证整页响应不被拖垮(本任务核心不变式)。

    `agent_id` 拼进 hub URL 路径段,**必须** `quote(safe="")` 编码(纵深防御第二道闸,
    仿 `rotate_agent_key`:含 `/` `..` `#` `?` 的 code 会改变请求路径 → 存储型路径注入)。
    失败(配置缺失 / 超时 / HTTP 错)向上抛,由路由层 catch → 该行 degraded。
    """
    if not settings.service_hub_url:
        raise HubError("SERVICE_HUB_URL 未配置")
    r = httpx.post(
        f"{settings.service_hub_url}/api/agents/{quote(agent_id, safe='')}/list-instances",
        headers=_headers(),
        json={"serviceName": service_name},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def rotate_agent_key(agent_id: str) -> str:
    """轮换指定 Agent 的连接密钥,返回新的 agentKey(旧密钥在 hub 侧立即失效)。

    `agent_id`(= namespace.code)拼进 hub URL 路径段,**必须** `quote(safe="")` 编码,
    否则含 `/` `..` `#` `?` 的 code 会改变请求路径,造成存储型路径注入(评审 A3:
    code='x/../../dispatch' 实测打到 hub dispatch=全机群 RCE)。NamespaceIn.code 已加白名单
    做第一道闸,这里编码是纵深防御第二道闸(双管齐下)。
    """
    if not settings.service_hub_url:
        raise HubError("SERVICE_HUB_URL 未配置")
    r = httpx.post(
        f"{settings.service_hub_url}/api/agents/{quote(agent_id, safe='')}/credentials/rotate",
        headers=_headers(),
        json={},
        timeout=_HUB_TIMEOUT,
    )
    r.raise_for_status()
    return _extract_agent_key(r)  # 复审 R3:非 JSON / 非 dict / 缺 key 统一归一化为 HubError
