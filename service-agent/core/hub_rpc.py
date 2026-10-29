"""
hub_rpc.py — 与交付中枢的一问一答（agent 半边）：本机 HTTP 端点把请求转成 WS 请求帧发给中枢，
同步等同 requestId 的结果帧，折成 (HTTP 状态码, 响应体) 返回。
请求帧 env_config_query / instance_snapshot_query，结果帧 env_config_result / instance_snapshot_result（RESULT_FRAMES）。

结果不进出站队列：这是一问一答，断连即失败（同 ops_code）。
传输层（连接代次、特性、在途表、发送线程和队列）在 core/hub_channel.py，与 ops_code 共用一份实现；
本模块的 _channel 是它的一个实例，本模块只负责：
- 帧：每种请求帧对应哪种结果帧、响应体在结果帧的哪个字段，登记在 RESULTS。结果帧必须和发出的请求对得上，
  类型不是这一种、或者响应体字段不是对象，一律按 502 unavailable 处理，免得错的对象以 200 返回。
- 协商期间要等：刚连上、hub_hello 还没到的几秒内进来的请求不立即判 501（否则应用会误以为中枢太旧），
  而是在 NEGOTIATE_SECONDS（从连上算起）内等协商结果；timeout 是含等协商在内的总时限。
- 在途上限 MAX_IN_FLIGHT（含正在等协商的），超出直接返回 busy：中枢慢或卡住时请求不会越积越多。
- 发送队列不设排队超时（通道的 stale_seconds 为 None）：只丢旧连接的、调用方已放弃的帧。
  调用方都有总时限（快照调用 10 秒），固定的排队超时反而会误丢仍在等的帧。
- 状态码和响应体的映射（见 call）。
日志不记请求和结果的内容：发送异常的信息里可能带着整帧，通道只记帧类型和异常类型。
"""
import logging
import time
import uuid

from core.hub_channel import DISCONNECTED, HubChannel

logger = logging.getLogger(__name__)

MAX_IN_FLIGHT = 8
SEND_QUEUE_SIZE = 8
NEGOTIATE_SECONDS = 5.0       # 连上后等 hub_hello 的时限（从连上算起）；超过仍没有就当旧版中枢
# 请求帧 → (期望的结果帧类型, 结果帧里装响应体的字段)。加新的一问一答只需在这里登记一行，ws_client 的分发跟着走
RESULTS = {
    'env_config_query': ('env_config_result', 'env'),
    'instance_snapshot_query': ('instance_snapshot_result', 'snapshot'),
}
RESULT_FRAMES = frozenset(frame for frame, _ in RESULTS.values())   # ws_client 据此分发给 resolve
ERROR_STATUS = {'not_found': 404, 'not_bound': 403, 'unavailable': 502}        # 结果帧 error → HTTP 状态码

_monotonic = time.monotonic   # 测试替换这个时钟，不动全局 time.monotonic
_channel = HubChannel('hub-rpc', SEND_QUEUE_SIZE, clock=lambda: _monotonic())

# ws_client 在连接生命周期里调用这几个，语义见 HubChannel 的同名方法
on_connected = _channel.on_connected        # _on_open：新连接建立，换代，特性置为未知，换发送函数
set_features = _channel.set_features        # _on_hub_hello：记下本连接的特性
on_disconnected = _channel.on_disconnected  # _on_close：换代、清特性，立即唤醒在途请求返回 hub_disconnected
reset = _channel.reset                      # 测试用：清空连接、特性、在途请求和发送队列


def _reason(status, reason):
    return status, {'reason': reason}


def resolve(data: dict) -> None:
    """收到结果帧（ws_client 对 RESULT_FRAMES 里的类型调用）：按 requestId 交给对应的在途请求。
    未知、已结束（超时放弃、已有结果或已断线）、旧连接代次的结果一律丢弃。"""
    _channel.resolve(data)


def call(frame_type: str, fields: dict, feature: str, timeout: float) -> tuple[int, dict | None]:
    """发 {type: frame_type, requestId, **fields} 给中枢，同步等同 requestId 的结果帧，返回 (HTTP 状态码, 响应体)。

    feature 是中枢须在 hub_hello.features 里声明的特性名；timeout 是整个调用（含等协商）的总时限，单位秒。
      没连上中枢 / 等待期间断线             → 503 {"reason": "hub_disconnected"}
      连上后 NEGOTIATE_SECONDS 内没收到 hub_hello，或特性里没有 feature
                                           → 501 {"reason": "hub_unsupported"}
      在途已有 MAX_IN_FLIGHT 个             → 503 {"reason": "busy"}
      超时                                 → 504 {"reason": "timeout"}
      结果帧 error 为 not_found / not_bound / unavailable → 404 / 403 / 502 和 {"reason": error}
      结果帧有 env 或 snapshot              → 200 和那个对象（env 帧回 env 的值，snapshot 帧回 snapshot 的值）
      结果帧和请求对不上（类型不符、残缺、认不得的错误码） → 502 {"reason": "unavailable"}
    frame_type 必须登记在 RESULTS 里，否则抛 ValueError（调用方的编程错误）。
    """
    expect = RESULTS.get(frame_type)
    if expect is None:
        raise ValueError(f'未登记的请求帧类型：{frame_type}')
    request_id = uuid.uuid4().hex
    deadline = _monotonic() + timeout
    with _channel.lock:
        if not _channel.connected:
            return _reason(503, 'hub_disconnected')
        if _channel.features is not None and feature not in _channel.features:
            return _reason(501, 'hub_unsupported')
        entry = _channel.register(request_id, MAX_IN_FLIGHT)   # 登记即占一个在途名额：等协商的请求也算，免得连上后的几秒内无限堆积
        if entry is None:
            return _reason(503, 'busy')
        entry['expect'] = expect          # 结果帧要和请求对得上：类型是这一种，响应体在这个字段里
    try:
        return _exchange(request_id, entry, frame_type, fields, feature, deadline)
    finally:
        _channel.release(request_id)


def _exchange(request_id, entry, frame_type, fields, feature, deadline):
    """已登记的请求的后半程：等协商 → 入队发送 → 等结果。"""
    with _channel.lock:
        if _channel.features is None and not entry['event'].is_set():
            window_end = _channel.connected_at + NEGOTIATE_SECONDS    # 条目没被唤醒过，说明仍是登记时的那条连接
            _channel.wait_features(entry, min(window_end, deadline))
            if _channel.features is None and not entry['event'].is_set():
                # 等满了仍没有 hub_hello：协商窗口先到是旧版中枢（501），调用方的总时限先到只是超时（504）
                return _reason(501, 'hub_unsupported') if window_end <= deadline else _reason(504, 'timeout')
        if entry['event'].is_set():          # 等协商时（或登记之后、入队之前）断线或换了连接
            return _outcome(entry)
        if feature not in _channel.features:
            return _reason(501, 'hub_unsupported')
        frame = {**fields, 'type': frame_type, 'requestId': request_id}   # 信封放最后：fields 夹带同名键也盖不掉
        if not _channel.enqueue(request_id, entry, frame):
            return _reason(503, 'busy')
    if not entry['event'].wait(max(0.0, deadline - _monotonic())):
        return _reason(504, 'timeout')
    return _outcome(entry)


def _outcome(entry):
    """把定下的结果折成 (HTTP 状态码, 响应体)。"""
    result = entry['result']
    if result is DISCONNECTED:
        return _reason(503, 'hub_disconnected')
    frame_type, body_key = entry['expect']
    matches = result.get('type') == frame_type
    if matches:                           # 结果帧得是这个请求对应的那一种，否则字段再像也不能信
        error = result.get('error')
        if error is None and isinstance(result.get(body_key), dict):
            return 200, result[body_key]
        if isinstance(error, str) and error in ERROR_STATUS:
            return _reason(ERROR_STATUS[error], error)
    # 类型对不上、认不得的错误码（中枢以后可能新增）或残缺的结果帧：只记字段名，不记内容
    logger.warning('中枢的结果帧不合规（期望 %s，类型%s，字段：%s），按 unavailable 处理',
                   frame_type, '相符' if matches else '不符', ','.join(sorted(map(str, result))) or '无')
    return _reason(502, 'unavailable')
