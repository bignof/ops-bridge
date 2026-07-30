from pathlib import Path
import socket
import sys


import pytest

ROOT = Path(__file__).resolve().parent.parent
root_str = str(ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)


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