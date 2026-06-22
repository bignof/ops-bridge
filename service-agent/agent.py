import logging
import time

from config import RECONNECT_DELAY
from core.health_server import start_health_server
from core.plugin_server import maybe_start_plugin_server
from core.ws_client import connect

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    start_health_server()
    maybe_start_plugin_server()  # 配齐插件分发才起 worker-facing server（可选能力）
    while True:
        connect()
        logger.info(f"Reconnecting in {RECONNECT_DELAY} seconds...")
        time.sleep(RECONNECT_DELAY)
