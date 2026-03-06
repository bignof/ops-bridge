import logging
import time

from config import RECONNECT_DELAY
from core.ws_client import connect

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    while True:
        connect()
        logger.info(f"Reconnecting in {RECONNECT_DELAY} seconds...")
        time.sleep(RECONNECT_DELAY)
