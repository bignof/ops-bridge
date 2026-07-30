"""
outbox.py — result 出站队列（至少一次投递的 agent 半边）

背景：命令在独立线程执行，线程闭包持有下发时刻的 ws；长命令（update 拉镜像挤占带宽致
协议层 pong 超时断连重连）执行完后把 result 发进已关闭的旧连接，hub 永远收不到、命令
悬挂「执行中」。本模块把 result 记账为「未确认」，收到 hub 的 result_ack 才清账；
重连（force flush）与心跳（退避 flush）负责补投；磁盘持久化抗 agent 自身重启。

ack（processing）不记账——丢了无害，hub 侧可从 pending 直接落终态。
兼容旧 hub（不回 result_ack）：退避重发至 MAX_ATTEMPTS 后放弃（hub 超时兜底接手），
不会无限重发刷噪音。
"""
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = int(os.getenv('OUTBOX_MAX_ATTEMPTS', '40'))

_lock = threading.Lock()
_items: dict[str, dict] = {}  # requestId -> {'message': dict, 'attempts': int, 'next_retry_ts': float}
_sender = None
_path: str | None = None


def _backoff_seconds(attempts: int) -> float:
    """重发退避：30s * 已尝试次数，封顶 5 分钟——旧 hub 不回 ack 时噪音密度可控。"""
    return min(30.0 * max(attempts, 1), 300.0)


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _save_locked() -> None:
    if not _path:
        return
    try:
        _ensure_parent_dir(_path)
        payload = {rid: {'message': item['message'], 'attempts': item['attempts']} for rid, item in _items.items()}
        tmp = f"{_path}.tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())  # 掉电也不丢最新账本(replace 原子性只防损坏,不防页缓存丢失)
        os.replace(tmp, _path)  # 原子替换，写一半掉电不损坏正式文件
    except Exception as e:
        logger.error(f"Outbox persist failed: {e}")


def configure(path: str, reload: bool = False) -> None:
    """设置持久化路径并从磁盘恢复。**幂等**:同一路径已加载则不动内存——connect() 每轮重连都会调用,
    若每次都以磁盘为准,上一轮落盘失败(磁盘满等)的内存项会被清掉。仅进程首次/换路径时真正加载;
    reload=True 强制重载(测试模拟进程重启用)。"""
    global _path
    with _lock:
        if _path == path and not reload:
            return
        _path = path
        _items.clear()
        try:
            if os.path.exists(path):
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
                for rid, item in data.items():
                    _items[rid] = {
                        'message': item['message'],
                        'attempts': int(item.get('attempts', 0)),
                        'next_retry_ts': 0.0,
                    }
                if _items:
                    logger.info(f"Outbox restored {len(_items)} unacked result(s) from {path}")
        except Exception as e:
            logger.error(f"Outbox load failed ({path}), starting empty: {e}")
            _items.clear()


def set_sender(sender) -> None:
    """注册当前活跃连接的发送函数（_on_open 调用；重连即换新连接）。"""
    global _sender
    with _lock:
        _sender = sender


def clear_sender() -> None:
    global _sender
    with _lock:
        _sender = None


def remember(message: dict) -> None:
    """result 记账（入队+落盘）。立即发送由调用方负责——记账在前，发丢了也能补投。"""
    request_id = message.get('requestId')
    if not request_id:
        return
    with _lock:
        _items[request_id] = {'message': message, 'attempts': 0, 'next_retry_ts': 0.0}
        _save_locked()


def ack(request_id) -> None:
    """hub 已确认落库：清账。未知 requestId（已被上限丢弃等）忽略。"""
    if not request_id:
        return
    with _lock:
        if _items.pop(request_id, None) is not None:
            logger.info(f"Result acked by hub: request_id={request_id}")
            _save_locked()


def pending_count() -> int:
    with _lock:
        return len(_items)


def flush(force: bool = False, now: float | None = None) -> None:
    """补投未确认 result。attempts 语义 = 「经健康连接真正发出的次数」:
    - 发送抛异常不计数(sender 必须用会抛错的裸 ws.send,不能用吞异常的包装);
    - force=True(重连补投)不计数——重连风暴/鉴权失败循环下不消耗投递预算,
      只有心跳周期(连接存续)的发出才累计,MAX_ATTEMPTS(默认40,退避后≈3小时)
      仅用于「连接健康但 hub 始终不回 ack」(旧 hub)的有限放弃;
    - 发送在锁外进行(网络 IO 不阻塞执行线程的 remember/ack)。"""
    if now is None:
        now = time.time()
    with _lock:
        sender = _sender
        if sender is None:
            return
        expired = [rid for rid, item in _items.items() if item['attempts'] >= MAX_ATTEMPTS]
        for rid in expired:
            item = _items.pop(rid)
            logger.error(f"Outbox gave up after {item['attempts']} attempts (hub never acked): request_id={rid}")
        if expired:
            _save_locked()
        due = [(rid, item['message']) for rid, item in _items.items() if force or now >= item['next_retry_ts']]
    # 锁外发送:ws.send 可能阻塞/抛错,不能拖住持锁的 remember/ack 调用方
    sent_ok: list[str] = []
    for rid, message in due:
        try:
            sender(message)
            sent_ok.append(rid)
        except Exception as e:
            logger.warning(f"Outbox send failed (will retry): request_id={rid}, error={e}")
    if not sent_ok:
        return
    with _lock:
        for rid in sent_ok:
            item = _items.get(rid)
            if item is None:
                continue  # 发送窗口内已被 ack 清账
            if force:
                # 重连补投:不消耗预算,只推迟下一次心跳重发,避免紧跟的心跳立刻重复
                item['next_retry_ts'] = now + _backoff_seconds(max(item['attempts'], 1))
            else:
                item['attempts'] += 1
                item['next_retry_ts'] = now + _backoff_seconds(item['attempts'])
            logger.info(f"Outbox (re)sent result: request_id={rid}, attempts={item['attempts']}, force={force}")
        _save_locked()
