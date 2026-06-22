"""force 操作服务端护栏(速率 + 不可停最后健康实例)。

两道闸,均在 hub 服务端强制、不可被 UI 绕过,仅作用于 force stop:

① 全局滑窗速率限制(进程内,hub 单实例前提):超限抛 429,命令不入库不下发。
② 停前调 agent list-instances 核实健康实例数;若停掉后该 service 健康实例会归零
   (即当前 healthy ≤ 1)→ 拒(409),除非显式 allowLastInstance。核实不到健康数
   (list-instances 失败 / agent 不可达)采取 fail-closed:拒(409),宁可挡住也不误停。

实现说明:
- 滑窗用模块级 deque + threading.Lock + time.monotonic()。加锁块内无 IO、瞬时完成,
  FastAPI async handler 里同步调用不会阻塞事件循环。monotonic 不受系统墙钟回拨影响。
- 阈值在"调用时"读 settings,便于测试 monkeypatch settings 后立即生效。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import TYPE_CHECKING

from fastapi import HTTPException, status

if TYPE_CHECKING:  # 仅类型提示,避免运行时硬依赖具体类型
    from app.hub.config import Settings

# 模块级滑窗:记录每次"通过"的 force 操作时间戳(time.monotonic 秒)。
_force_op_timestamps: deque[float] = deque()
_force_op_lock = threading.Lock()


def check_force_rate_limit(settings: "Settings") -> None:
    """① 全局滑窗速率闸。超限抛 429;否则记录本次时间戳后返回。

    调用时读 settings.force_op_max_per_window / force_op_window_sec,便于测试覆盖阈值。
    """
    max_per_window = settings.force_op_max_per_window
    window_sec = settings.force_op_window_sec
    now = time.monotonic()
    with _force_op_lock:
        # 剪掉早于窗口的时间戳(deque 左侧为最旧)。
        boundary = now - window_sec
        while _force_op_timestamps and _force_op_timestamps[0] <= boundary:
            _force_op_timestamps.popleft()
        if len(_force_op_timestamps) >= max_per_window:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="force 操作过于频繁,已超速率限制(请稍后或显式解锁)",
            )
        _force_op_timestamps.append(now)


def reset_force_rate_limit() -> None:
    """清空滑窗(供测试隔离用例间状态)。"""
    with _force_op_lock:
        _force_op_timestamps.clear()


async def check_last_healthy_instance(hub_state, agent_id: str, service_name: str, timeout: float) -> None:
    """② 不可停掉某 service 最后一个健康实例。

    调 agent list-instances 统计健康实例数:
    - 无法核实(list-instances 返回非 success,或 agent 不可达 / 调用抛异常)→ fail-closed 拒(409)。
    - 健康实例数 ≤ 1(停掉后归零)→ 拒(409)。
    - 健康实例数 ≥ 2 → 放行(返回)。
    """
    import uuid

    req = str(uuid.uuid4())
    try:
        listed = await hub_state.call_agent(
            agent_id,
            {"type": "list-instances", "requestId": req, "serviceName": service_name},
            timeout=timeout,
        )
    except Exception:
        # agent 不可达 / 超时 / 其它:同样无法核实,fail-closed。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="无法核实健康实例数,拒绝 force stop(可重试或设 allowLastInstance)",
        )

    if not isinstance(listed, dict) or listed.get("status") != "success":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="无法核实健康实例数,拒绝 force stop(可重试或设 allowLastInstance)",
        )

    instances = listed.get("instances") or []
    healthy = [i for i in instances if i.get("healthy")]
    if len(healthy) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="不可停掉该 service 最后一个健康实例(如确需请设 allowLastInstance)",
        )
