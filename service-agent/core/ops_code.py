"""
ops_code.py — 运维口令校验：业务服务经本机 /verifyOpsCode 提交，Agent 转成 ops_code_verify 帧发给中枢并同步等结果。

结果不进出站队列：这是一问一答，断连即失败（PROTOCOL.md 第 10 节）。
- 特性：每条新连接先置为「未知」，收到 hub_hello 后记下 features，断线清空。
- 在途上限 MAX_IN_FLIGHT，超出直接返回 busy。
- 发送：一个专用发送线程配有界队列。半开连接下 send 阻塞时只卡住这一个线程；
  排队超过 SEND_STALE_SECONDS 的帧、旧连接的帧、调用方已放弃的帧都直接丢弃。
- 连接代次：连上、断开都让代次加一；断开时立即唤醒全部在途请求，迟到的旧代次结果丢弃。
日志绝不记口令：发送异常的信息里可能带着整帧，只记异常类型。
"""
import logging
import queue
import threading
import time
import uuid

logger = logging.getLogger(__name__)

FEATURE = 'ops_code'          # 中枢在 hub_hello.features 里声明支持本协议
MAX_IN_FLIGHT = 8
SEND_QUEUE_SIZE = 8
SEND_STALE_SECONDS = 8.0
NEGOTIATE_SECONDS = 5.0
RESULT_TIMEOUT = 8.0          # 须小于服务端调 /verifyOpsCode 的 12 秒超时

_DISCONNECTED = object()
_monotonic = time.monotonic   # 测试替换这个时钟，不动全局 time.monotonic

_lock = threading.Lock()
_generation = 0
_connected_at = None          # 当前连接建立时刻（_monotonic）；None = 未连接
_features = None              # None = 当前连接还没收到 hub_hello
_sender = None
_pending = {}                 # requestId -> {'event', 'generation', 'result'}
_queue = queue.Queue(maxsize=SEND_QUEUE_SIZE)
_sender_thread = None


def on_connected(sender):
    """新连接建立（ws_client._on_open）：换代，特性置为未知，换发送函数。"""
    global _generation, _connected_at, _features, _sender
    with _lock:
        _generation += 1
        _connected_at = _monotonic()
        _features = None
        _sender = sender


def set_features(features):
    """收到 hub_hello：记下本连接的特性；features 不是列表时按「没有任何特性」处理。断线后迟到的调用忽略。"""
    global _features
    with _lock:
        if _connected_at is None:
            return
        names = features if isinstance(features, list) else []
        _features = frozenset(name for name in names if isinstance(name, str))


def on_disconnected():
    """连接断开（ws_client._on_close）：换代、清特性，立即唤醒全部在途请求返回 disconnected。"""
    global _generation, _connected_at, _features, _sender
    with _lock:
        _generation += 1
        _connected_at = None
        _features = None
        _sender = None
        for entry in _pending.values():
            entry['result'] = _DISCONNECTED
            entry['event'].set()


def resolve(data):
    """收到 ops_code_verify_result：交给对应的在途请求。未知、已结束或旧连接的结果丢弃，返回 False。"""
    request_id = data.get('requestId')
    with _lock:
        entry = _pending.get(request_id) if isinstance(request_id, str) else None
        if entry is None or entry['generation'] != _generation or entry['event'].is_set():
            return False
        entry['result'] = {key: value for key, value in data.items() if key not in ('type', 'requestId')}
        entry['event'].set()
        return True


def verify(fields, timeout=RESULT_TIMEOUT):
    """提交一次校验并等待结果，返回 (HTTP 状态码, 响应体或 None)。fields 已由 /verifyOpsCode 校验。"""
    request_id = uuid.uuid4().hex
    with _lock:
        if _sender is None or _connected_at is None:
            return 503, {'reason': 'unreachable'}
        if _features is None:
            if _monotonic() - _connected_at < NEGOTIATE_SECONDS:
                return 503, {'reason': 'negotiating'}
            return 501, None          # 连上 5 秒仍没收到 hub_hello：旧版中枢
        if FEATURE not in _features:
            return 501, None
        if len(_pending) >= MAX_IN_FLIGHT:
            return 503, {'reason': 'busy'}
        entry = {'event': threading.Event(), 'generation': _generation, 'result': None}
        _pending[request_id] = entry
        frame = {'type': 'ops_code_verify', 'requestId': request_id, **fields}
        try:
            _queue.put_nowait((_monotonic(), _generation, request_id, frame))
        except queue.Full:
            _pending.pop(request_id, None)
            return 503, {'reason': 'busy'}
        _ensure_sender_thread()
    try:
        if not entry['event'].wait(timeout):
            return 504, None
        if entry['result'] is _DISCONNECTED:
            return 503, {'reason': 'disconnected'}
        return 200, entry['result']
    finally:
        with _lock:
            _pending.pop(request_id, None)


def _send_one(item):
    """发送线程处理一帧：旧连接的、调用方已放弃的、排队过久的直接丢弃。返回是否已发出。"""
    enqueued_at, generation, request_id, frame = item
    with _lock:
        sender = _sender if generation == _generation and request_id in _pending else None
    if sender is None or _monotonic() - enqueued_at > SEND_STALE_SECONDS:
        return False
    try:
        sender(frame)
        return True
    except Exception as exc:  # noqa: BLE001 —— 发送失败按断连处理；异常信息可能含整帧，只记类型
        logger.warning('ops_code_verify 发送失败（%s），按断连处理', type(exc).__name__)
        _fail(request_id, generation)
        return False


def _fail(request_id, generation):
    with _lock:
        entry = _pending.get(request_id)
        if entry is not None and entry['generation'] == generation and not entry['event'].is_set():
            entry['result'] = _DISCONNECTED
            entry['event'].set()


def _send_loop():
    while True:
        _send_one(_queue.get())


def _ensure_sender_thread():
    """在 _lock 内调用：发送线程不存在或已退出时启动一个，全程至多一个。"""
    global _sender_thread
    if _sender_thread is None or not _sender_thread.is_alive():
        _sender_thread = threading.Thread(target=_send_loop, name='ops-code-sender', daemon=True)
        _sender_thread.start()


def reset():
    """测试用：清空连接、特性、在途请求和发送队列。发送线程保留，队列空了它就阻塞在 get 上。"""
    global _generation, _connected_at, _features, _sender
    with _lock:
        for entry in _pending.values():
            entry['result'] = _DISCONNECTED
            entry['event'].set()
        _pending.clear()
        _generation += 1
        _connected_at = None
        _features = None
        _sender = None
        while True:
            try:
                _queue.get_nowait()
            except queue.Empty:
                break
