"""
plugin_query.py — admin 经本机 agent 拉插件清单的 agent 半边（同步请求-响应）。

health_server 的 /queryPlugin（HTTP 工作线程）调 request()：向 hub 发 plugin_query、阻塞等
plugin_query_result；ws_client 的 _on_message（WS 读线程）收到响应调 resolve() 唤醒等待方。
仿 outbox 的 sender 注册范式，但语义是同步一问一答，不入 outbox（不做断连补投，断连即失败让
上游 sync-plugins.js 回退本地版本）。pending 表必须有界：request 用 try/finally 无条件 pop。
应用要环境开关时（with_env）请求帧带 withEnv，hub 顺带回 env；旧 hub 不认、不回，调用方拿到的仍是纯数组。
"""
import logging
import threading
import uuid

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pending: dict[str, dict] = {}  # requestId -> {'event': Event, 'result': list | None, 'error': str | None, 'env': dict | None}
# hub 明确拒绝的原因；旧 hub 不带此字段。not_bound = 这台服务器没有绑定到应用所在的环境，
# hub 只发给上报了 plugin_query_env 能力的 agent（其余 agent 收到 not_found）
ERRORS = {'not_found', 'unavailable', 'not_bound'}
_sender = None


def set_sender(sender) -> None:
    """注册当前活跃连接的发送函数（_on_open 调用；重连即换新连接）。"""
    global _sender
    with _lock:
        _sender = sender


def clear_sender() -> None:
    global _sender
    with _lock:
        _sender = None


def request(service: str, timeout: float, with_env: bool = False):
    """发 plugin_query、阻塞等 plugin_query_result。with_env 为真时请求帧带 withEnv: true，让 hub 顺带回环境开关。

    返回纯数组、None（无连接/发送失败/超时）、QueryError（hub 找不到服务、服务器未绑定或生成清单失败），
    或在 with_env 为真且 hub 回了合规 env 时返回 {'plugins': [...], 'env': {'allowUi': bool, 'allowDs': bool}}。
    旧 hub 不认 withEnv、不回 env，env 形状不对的也当没回：仍是纯数组，由应用侧当作环境开关未知。
    不带 with_env 的老调用方永远得到纯数组，哪怕 hub 回了 env。
    """
    with _lock:
        sender = _sender
        if sender is None:
            return None
        request_id = uuid.uuid4().hex
        event = threading.Event()
        _pending[request_id] = {'event': event, 'result': None, 'error': None, 'env': None}
    try:
        frame = {'type': 'plugin_query', 'requestId': request_id, 'service': service, 'manifestVersion': 2}
        if with_env:
            frame['withEnv'] = True

        def _send():
            try:
                sender(frame)
            except Exception as e:
                logger.warning(f"plugin_query send failed: {e}")
                event.set()  # 早失败早唤醒（result 仍为 None → 上游按超时/失败处理）

        # 评审 M2：裸 ws.send 全程无 socket 超时（半开连接可阻塞到 OS TCP 重传上限，远超 timeout），
        # 放 daemon 线程执行，让下面的 event.wait(timeout) 真正约束调用方总耗时
        threading.Thread(target=_send, daemon=True).start()
        if not event.wait(timeout):
            logger.warning(f"plugin_query timeout: service={service}, requestId={request_id}")
            return None
        with _lock:
            entry = _pending.get(request_id)
            if entry and entry['error']:
                return QueryError(entry['error'])
            if entry and with_env and entry['env'] is not None:
                return {'plugins': entry['result'], 'env': entry['env']}
            return entry['result'] if entry else None
    finally:
        with _lock:
            _pending.pop(request_id, None)


class QueryError:
    """hub 明确拒绝本次拉取（reason 取自 ERRORS）；/queryPlugin 返回非 200，让 sync-plugins 记为失败而不是「没有插件」。"""

    def __init__(self, reason: str):
        self.reason = reason


def _valid_env(env) -> bool:
    return isinstance(env, dict) and isinstance(env.get('allowUi'), bool) and isinstance(env.get('allowDs'), bool)


def resolve(request_id, plugins, error=None, env=None) -> None:
    """收到 plugin_query_result：填结果并唤醒。未命中（超时已清/未知）直接 no-op。
    env 只在是含布尔 allowUi、allowDs 的对象时保存，否则当作 hub 没回（旧 hub，或形状不对）。"""
    if not request_id:
        return
    with _lock:
        entry = _pending.get(request_id)
        if entry is None:
            return
        entry['result'] = plugins if isinstance(plugins, list) else []
        entry['error'] = error if error in ERRORS else None
        entry['env'] = env if _valid_env(env) else None
        entry['event'].set()
