"""
plugin_query.py — admin 经本机 agent 拉插件清单的 agent 半边（同步请求-响应）。

health_server 的 /queryPlugin（HTTP 工作线程）调 request()：向 hub 发 plugin_query、阻塞等
plugin_query_result；ws_client 的 _on_message（WS 读线程）收到响应调 resolve() 唤醒等待方。
仿 outbox 的 sender 注册范式，但语义是同步一问一答，不入 outbox（不做断连补投，断连即失败让
上游 sync-plugins.js 回退本地版本）。pending 表必须有界：request 用 try/finally 无条件 pop。
"""
import logging
import threading
import uuid

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pending: dict[str, dict] = {}  # requestId -> {'event': Event, 'result': list | None}
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


def request(service: str, timeout: float):
    """发 plugin_query、阻塞等 plugin_query_result。返回纯数组或 None（无连接/发送失败/超时）。"""
    with _lock:
        sender = _sender
        if sender is None:
            return None
        request_id = uuid.uuid4().hex
        event = threading.Event()
        _pending[request_id] = {'event': event, 'result': None}
    try:
        def _send():
            try:
                sender({'type': 'plugin_query', 'requestId': request_id, 'service': service})
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
            return entry['result'] if entry else None
    finally:
        with _lock:
            _pending.pop(request_id, None)


def resolve(request_id, plugins) -> None:
    """收到 plugin_query_result：填结果并唤醒。未命中（超时已清/未知）直接 no-op。"""
    if not request_id:
        return
    with _lock:
        entry = _pending.get(request_id)
        if entry is None:
            return
        entry['result'] = plugins if isinstance(plugins, list) else []
        entry['event'].set()
