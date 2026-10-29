"""
ops_code.py — 运维口令校验：业务服务经本机 /verifyOpsCode 提交，Agent 转成 ops_code_verify 帧发给中枢并同步等结果。

结果不进出站队列：这是一问一答，断连即失败（PROTOCOL.md 第 10 节）。
传输层（连接代次、特性、在途表、发送线程和队列）在 core/hub_channel.py，与 hub_rpc 共用一份实现；
本模块的 _channel 是它的一个实例，本模块只负责：
- 帧和特性：ops_code_verify / ops_code_verify_result，中枢在 hub_hello.features 里声明 ops_code。
- 协商期间不等待：刚连上、hub_hello 还没到回 503 negotiating，连上 NEGOTIATE_SECONDS 仍没有就当旧版中枢回 501。
- 在途上限 MAX_IN_FLIGHT，超出直接返回 busy；发送队列里排队超过 SEND_STALE_SECONDS 的帧不再发送。
- 状态码和响应体的映射（见 verify）。
日志绝不记口令：发送异常的信息里可能带着整帧，通道只记帧类型和异常类型。
"""
import time
import uuid

from core.hub_channel import DISCONNECTED, HubChannel

FEATURE = 'ops_code'          # 中枢在 hub_hello.features 里声明支持本协议
MAX_IN_FLIGHT = 8
SEND_QUEUE_SIZE = 8
SEND_STALE_SECONDS = 8.0
NEGOTIATE_SECONDS = 5.0
RESULT_TIMEOUT = 8.0          # 须小于服务端调 /verifyOpsCode 的 12 秒超时

_monotonic = time.monotonic   # 测试替换这个时钟，不动全局 time.monotonic
_channel = HubChannel('ops-code', SEND_QUEUE_SIZE, stale_seconds=SEND_STALE_SECONDS, clock=lambda: _monotonic())

# ws_client 在连接生命周期里调用这几个，语义见 HubChannel 的同名方法
on_connected = _channel.on_connected        # _on_open：新连接建立，换代，特性置为未知，换发送函数
set_features = _channel.set_features        # _on_hub_hello：记下本连接的特性
on_disconnected = _channel.on_disconnected  # _on_close：换代、清特性，立即唤醒在途请求返回 disconnected
resolve = _channel.resolve                  # 收到 ops_code_verify_result：交给在途请求；迟到、未知、旧代次的丢弃返回 False
reset = _channel.reset                      # 测试用：清空连接、特性、在途请求和发送队列


def verify(fields, timeout=RESULT_TIMEOUT):
    """提交一次校验并等待结果，返回 (HTTP 状态码, 响应体或 None)。fields 已由 /verifyOpsCode 校验。"""
    request_id = uuid.uuid4().hex
    with _channel.lock:
        if not _channel.connected:
            return 503, {'reason': 'unreachable'}
        if _channel.features is None:
            if _monotonic() - _channel.connected_at < NEGOTIATE_SECONDS:
                return 503, {'reason': 'negotiating'}
            return 501, None          # 连上 5 秒仍没收到 hub_hello：旧版中枢
        if FEATURE not in _channel.features:
            return 501, None
        entry = _channel.register(request_id, MAX_IN_FLIGHT)
        if entry is None:
            return 503, {'reason': 'busy'}
        frame = {'type': 'ops_code_verify', 'requestId': request_id, **fields}
        if not _channel.enqueue(request_id, entry, frame):
            return 503, {'reason': 'busy'}
    try:
        if not entry['event'].wait(timeout):
            return 504, None
        if entry['result'] is DISCONNECTED:
            return 503, {'reason': 'disconnected'}
        return 200, {key: value for key, value in entry['result'].items() if key != 'type'}
    finally:
        _channel.release(request_id)
