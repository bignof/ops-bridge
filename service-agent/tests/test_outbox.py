import json
import logging

import pytest

from core import outbox


def test_remember_persists_and_reloads(tmp_path):
    msg = {"type": "result", "requestId": "req-1", "status": "success", "output": "ok"}
    outbox.remember(msg)
    assert outbox.pending_count() == 1

    # 已消耗的投递预算跨重启保留(心跳 flush 两次 → attempts=2)
    outbox.set_sender(lambda m: None)
    outbox.flush(now=1000.0)
    outbox.flush(now=1000.0 + 3600.0)

    # 模拟 agent 进程重启:强制重载同一路径 → 从磁盘恢复
    outbox.configure(str(tmp_path / "outbox-autouse.json"), reload=True)
    assert outbox.pending_count() == 1
    assert outbox._items["req-1"]["attempts"] == 2  # 预算不因重启清零

    # 磁盘内容是合法 JSON 且含消息本体
    data = json.loads((tmp_path / "outbox-autouse.json").read_text(encoding="utf-8"))
    assert data["req-1"]["message"]["output"] == "ok"


def test_ack_removes_and_persists(tmp_path):
    outbox.remember({"type": "result", "requestId": "req-1", "status": "success"})
    outbox.remember({"type": "result", "requestId": "req-2", "status": "failed"})
    outbox.ack("req-1")
    assert outbox.pending_count() == 1
    outbox.ack("ghost")  # 未知 requestId 忽略
    assert outbox.pending_count() == 1

    outbox.configure(str(tmp_path / "outbox-autouse.json"), reload=True)
    assert outbox.pending_count() == 1


def test_configure_same_path_is_idempotent(tmp_path):
    """connect() 每轮重连都会 configure:同路径不得清掉内存中未落盘/新入队的项。"""
    outbox.remember({"type": "result", "requestId": "req-1", "status": "success"})
    outbox.configure(str(tmp_path / "outbox-autouse.json"))  # conftest 已设的同一路径
    assert outbox.pending_count() == 1


def test_flush_requires_sender_and_respects_backoff():
    sent: list[dict] = []
    outbox.remember({"type": "result", "requestId": "req-1", "status": "success"})

    outbox.flush(now=1000.0)  # 无 sender:不发不崩
    assert sent == []

    outbox.set_sender(lambda m: sent.append(m))
    outbox.flush(now=1000.0)  # 首次到期(next_retry 初始为 0)→ 发送
    assert [m["requestId"] for m in sent] == ["req-1"]

    outbox.flush(now=1001.0)  # 退避窗口内不重发
    assert len(sent) == 1

    outbox.flush(now=1000.0 + 24 * 3600)  # 远超退避 → 重发
    assert len(sent) == 2


def test_reconnect_force_flush_ignores_backoff():
    sent: list[dict] = []
    outbox.remember({"type": "result", "requestId": "req-1", "status": "success"})
    outbox.set_sender(lambda m: sent.append(m))
    outbox.flush(now=1000.0)
    outbox.flush(now=1000.5, force=True)  # 重连补投:忽略退避立即再发
    assert len(sent) == 2


def test_flush_drops_after_max_attempts(caplog: pytest.LogCaptureFixture):
    """只有心跳周期(非 force)的成功发出消耗预算;达到上限后淘汰(hub 超时兜底接手)。"""
    caplog.set_level(logging.ERROR)
    outbox.remember({"type": "result", "requestId": "req-x", "status": "success"})
    outbox.set_sender(lambda m: None)
    for i in range(outbox.MAX_ATTEMPTS + 5):
        outbox.flush(now=1000.0 + i * 3600.0)  # 心跳 flush,每次远超退避
    assert outbox.pending_count() == 0
    assert "req-x" in caplog.text


def test_force_flush_does_not_consume_attempts():
    """重连补投(force)不消耗投递预算——鉴权失败/轮换 key 的重连循环不会把 result 提前弃投。"""
    outbox.remember({"type": "result", "requestId": "req-f", "status": "success"})
    outbox.set_sender(lambda m: None)
    for i in range(outbox.MAX_ATTEMPTS * 3):
        outbox.flush(now=1000.0 + i * 5.0, force=True)  # 模拟 5s 间隔重连风暴
    assert outbox.pending_count() == 1  # 永不因 force 重连被淘汰


def test_send_failure_does_not_consume_attempts():
    """发送抛异常(连接已死)不计预算——预算只统计真正发出的次数。"""
    def broken(_m):
        raise RuntimeError("socket closed")

    outbox.remember({"type": "result", "requestId": "req-b", "status": "success"})
    outbox.set_sender(broken)
    for i in range(outbox.MAX_ATTEMPTS + 5):
        outbox.flush(now=1000.0 + i * 3600.0)
    assert outbox.pending_count() == 1  # 全部失败:仍在队列等真正的投递机会


def test_corrupt_persist_file_starts_empty(tmp_path, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.ERROR)
    bad = tmp_path / "bad-outbox.json"
    bad.write_text("{not-json", encoding="utf-8")
    outbox.configure(str(bad), reload=True)
    assert outbox.pending_count() == 0
    assert "Outbox load failed" in caplog.text


def test_backoff_caps_at_300s():
    assert outbox._backoff_seconds(1) == 30.0
    assert outbox._backoff_seconds(10) == 300.0
    assert outbox._backoff_seconds(40) == 300.0


def test_sender_error_keeps_item():
    def broken(_m):
        raise RuntimeError("socket closed")

    outbox.remember({"type": "result", "requestId": "req-1", "status": "success"})
    outbox.set_sender(broken)
    outbox.flush(now=1000.0)
    assert outbox.pending_count() == 1
