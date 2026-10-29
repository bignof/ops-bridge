"""
hub_rpc.py — 与交付中枢的一问一答（agent 半边）：本机 HTTP 端点把请求转成 WS 请求帧发给中枢，
同步等同 requestId 的结果帧，折成 (HTTP 状态码, 响应体) 返回。
请求帧 env_config_query / instance_snapshot_query，结果帧 env_config_result / instance_snapshot_result（RESULT_FRAMES）。

结果不进出站队列：这是一问一答，断连即失败（同 ops_code）。
- 特性：每条新连接先置为「未知」，收到 hub_hello 后记下 features，断线清空。刚连上、hub_hello 还没到的几秒内
  进来的请求不立即判 501（否则应用会误以为中枢太旧），而是在 NEGOTIATE_SECONDS（从连上算起）内等协商结果。
- 在途上限 MAX_IN_FLIGHT（含正在等协商的），超出直接返回 busy：中枢慢或卡住时请求不会越积越多。
- 发送：一个专用发送线程配有界队列。半开连接下 send 阻塞时只卡住这一个线程；旧连接的帧、调用方已放弃的帧
  直接丢弃（调用方都有总时限，超时即放弃，所以不会有排队过久还被发出去的帧）。
- 连接代次：连上、断开都让代次加一，并立即唤醒全部在途请求（返回 hub_disconnected）；迟到的旧代次结果丢弃。
  结果和断线同时发生时先到的说了算：已经收到结果的请求不会被随后的断线改写。
日志不记请求和结果的内容：发送异常的信息里可能带着整帧，只记帧类型和异常类型。
"""
import logging
import queue
import threading
import time
import uuid

logger = logging.getLogger(__name__)

MAX_IN_FLIGHT = 8
SEND_QUEUE_SIZE = 8
NEGOTIATE_SECONDS = 5.0       # 连上后等 hub_hello 的时限（从连上算起）；超过仍没有就当旧版中枢
RESULT_FRAMES = frozenset({'env_config_result', 'instance_snapshot_result'})   # ws_client 据此分发给 resolve
ERROR_STATUS = {'not_found': 404, 'not_bound': 403, 'unavailable': 502}        # 结果帧 error → HTTP 状态码
BODY_KEYS = ('env', 'snapshot')                                                # 结果帧里装响应体的字段

_DISCONNECTED = object()
_monotonic = time.monotonic   # 测试替换这个时钟，不动全局 time.monotonic

_lock = threading.Lock()
_cond = threading.Condition(_lock)   # 等协商的请求在这里睡；hub_hello 到、连接换代时叫醒
_generation = 0
_connected_at = None          # 当前连接建立时刻（_monotonic）；None = 未连接
_features = None              # None = 当前连接还没收到 hub_hello
_sender = None
_pending = {}                 # requestId -> {'event', 'generation', 'result'}
_queue = queue.Queue(maxsize=SEND_QUEUE_SIZE)
_sender_thread = None


def _reason(status, reason):
    return status, {'reason': reason}


def _settle(entry, result):
    """调用时持有 _lock：给在途请求定下结果并唤醒等待者。已有结果的不改写（先到的说了算）。"""
    if not entry['event'].is_set():
        entry['result'] = result
        entry['event'].set()


def _wake_all():
    """调用时持有 _lock：让还没有结果的在途请求返回 hub_disconnected，并叫醒在等协商的请求。"""
    for entry in _pending.values():
        _settle(entry, _DISCONNECTED)
    _cond.notify_all()


def on_connected(sender):
    """新连接建立（ws_client._on_open）：换代，特性置为未知，换发送函数。
    断线时旧请求已唤醒过；这里再唤醒一遍是兜底，免得没走到 on_disconnected 的旧请求干等到超时。"""
    global _generation, _connected_at, _features, _sender
    with _lock:
        _wake_all()
        _generation += 1
        _connected_at = _monotonic()
        _features = None
        _sender = sender


def set_features(features):
    """收到 hub_hello：记下本连接的特性并叫醒等协商的请求；features 不是列表时按「没有任何特性」处理。断线后迟到的调用忽略。"""
    global _features
    with _lock:
        if _connected_at is None:
            return
        names = features if isinstance(features, list) else []
        _features = frozenset(name for name in names if isinstance(name, str))
        _cond.notify_all()


def on_disconnected():
    """连接断开（ws_client._on_close）：换代、清特性，立即唤醒全部在途请求返回 hub_disconnected。"""
    global _generation, _connected_at, _features, _sender
    with _lock:
        _generation += 1
        _connected_at = None
        _features = None
        _sender = None
        _wake_all()


def resolve(data: dict) -> None:
    """收到结果帧（ws_client 对 RESULT_FRAMES 里的类型调用）：按 requestId 交给对应的在途请求。
    未知、已结束（超时放弃、已有结果或已断线）、旧连接代次的结果一律丢弃。"""
    request_id = data.get('requestId')
    with _lock:
        entry = _pending.get(request_id) if isinstance(request_id, str) else None
        if entry is None or entry['generation'] != _generation:
            return
        _settle(entry, {key: value for key, value in data.items() if key not in ('type', 'requestId')})


def call(frame_type: str, fields: dict, feature: str, timeout: float) -> tuple[int, dict | None]:
    """发 {type: frame_type, requestId, **fields} 给中枢，同步等同 requestId 的结果帧，返回 (HTTP 状态码, 响应体)。

    feature 是中枢须在 hub_hello.features 里声明的特性名；timeout 是整个调用（含等协商）的总时限，单位秒。
      没连上中枢 / 等待期间断线             → 503 {"reason": "hub_disconnected"}
      连上后 NEGOTIATE_SECONDS 内没收到 hub_hello，或特性里没有 feature
                                           → 501 {"reason": "hub_unsupported"}
      在途已有 MAX_IN_FLIGHT 个             → 503 {"reason": "busy"}
      超时                                 → 504 {"reason": "timeout"}
      结果帧 error 为 not_found / not_bound / unavailable → 404 / 403 / 502 和 {"reason": error}
      结果帧有 env 或 snapshot              → 200 和那个对象（不含 type、requestId）
    """
    request_id = uuid.uuid4().hex
    deadline = _monotonic() + timeout
    with _lock:
        if _sender is None or _connected_at is None:
            return _reason(503, 'hub_disconnected')
        if _features is not None and feature not in _features:
            return _reason(501, 'hub_unsupported')
        if len(_pending) >= MAX_IN_FLIGHT:
            return _reason(503, 'busy')
        entry = {'event': threading.Event(), 'generation': _generation, 'result': None}
        _pending[request_id] = entry     # 登记即占一个在途名额：等协商的请求也算，免得连上后的几秒内无限堆积
    try:
        return _exchange(request_id, entry, frame_type, fields, feature, deadline)
    finally:
        with _lock:
            _pending.pop(request_id, None)


def _exchange(request_id, entry, frame_type, fields, feature, deadline):
    """已登记的请求的后半程：等协商 → 入队发送 → 等结果。"""
    with _lock:
        if _features is None and not entry['event'].is_set():
            window_end = _connected_at + NEGOTIATE_SECONDS    # 条目没被唤醒过，说明仍是登记时的那条连接
            _cond.wait_for(lambda: _features is not None or entry['event'].is_set(),
                           max(0.0, min(window_end, deadline) - _monotonic()))
            if _features is None and not entry['event'].is_set():
                # 等满了仍没有 hub_hello：协商窗口先到是旧版中枢（501），调用方的总时限先到只是超时（504）
                return _reason(501, 'hub_unsupported') if window_end <= deadline else _reason(504, 'timeout')
        if entry['event'].is_set():          # 等协商时（或登记之后、入队之前）断线或换了连接
            return _outcome(entry['result'])
        if feature not in _features:
            return _reason(501, 'hub_unsupported')
        frame = {**fields, 'type': frame_type, 'requestId': request_id}   # 信封放最后：fields 夹带同名键也盖不掉
        try:
            _queue.put_nowait((entry['generation'], request_id, frame))
        except queue.Full:
            return _reason(503, 'busy')
        _ensure_sender_thread()
    if not entry['event'].wait(max(0.0, deadline - _monotonic())):
        return _reason(504, 'timeout')
    return _outcome(entry['result'])


def _outcome(result):
    """把定下的结果折成 (HTTP 状态码, 响应体)。"""
    if result is _DISCONNECTED:
        return _reason(503, 'hub_disconnected')
    error = result.get('error')
    if error is None:
        for key in BODY_KEYS:
            if isinstance(result.get(key), dict):
                return 200, result[key]
    elif isinstance(error, str) and error in ERROR_STATUS:
        return _reason(ERROR_STATUS[error], error)
    # 既没有有效的 error 也没有 env / snapshot：认不得的错误码（中枢以后可能新增）或残缺的结果帧，只记字段名
    logger.warning('中枢的结果帧不合规（字段：%s），按 unavailable 处理', ','.join(sorted(map(str, result))) or '无')
    return _reason(502, 'unavailable')


def _send_one(item):
    """发送线程处理一帧：旧连接的、调用方已放弃的帧直接丢弃。返回是否已发出。"""
    generation, request_id, frame = item
    with _lock:
        sender = _sender if generation == _generation and request_id in _pending else None
    if sender is None:
        return False
    try:
        sender(frame)
        return True
    except Exception as exc:  # noqa: BLE001 —— 发送失败按断连处理；异常信息可能含整帧，只记类型
        logger.warning('%s 发送失败（%s），按断连处理', frame.get('type'), type(exc).__name__)
        _fail(request_id, generation)
        return False


def _fail(request_id, generation):
    with _lock:
        entry = _pending.get(request_id)
        if entry is not None and entry['generation'] == generation:
            _settle(entry, _DISCONNECTED)


def _send_loop():
    while True:
        _send_one(_queue.get())


def _ensure_sender_thread():
    """在 _lock 内调用：发送线程不存在或已退出时启动一个，全程至多一个。"""
    global _sender_thread
    if _sender_thread is None or not _sender_thread.is_alive():
        _sender_thread = threading.Thread(target=_send_loop, name='hub-rpc-sender', daemon=True)
        _sender_thread.start()


def reset():
    """测试用：清空连接、特性、在途请求和发送队列。发送线程保留，队列空了它就阻塞在 get 上。"""
    global _generation, _connected_at, _features, _sender
    with _lock:
        _wake_all()
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
