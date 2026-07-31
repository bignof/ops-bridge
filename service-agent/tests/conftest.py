import os
from pathlib import Path
import socket
import sys


import pytest

ROOT = Path(__file__).resolve().parent.parent
root_str = str(ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)

# config.py 顶层强制要求这两个 env 存在(缺失即 sys.exit)。conftest.py 保证在所有测试模块
# collection 之前执行,给个占位默认值兜底——真正需要真实值的测试(见 test_ws_client.py 的
# _import_ws_client)会用 monkeypatch.setenv 显式覆盖,不受影响。
os.environ.setdefault('WS_URL', 'ws://test.invalid/ws/agent')
os.environ.setdefault('AGENT_KEY', 'test-key')


@pytest.fixture
def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(autouse=True)
def isolated_outbox_state(tmp_path):
    """outbox 是模块级单例:每个测试重定向持久化到临时目录,防跨测试污染与工作区落盘。"""
    from core import outbox

    outbox.configure(str(tmp_path / "outbox-autouse.json"))
    yield
    outbox.clear_sender()