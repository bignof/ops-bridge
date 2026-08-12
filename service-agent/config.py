import os
import socket
import sys
import logging
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()


CHINA_TZ = timezone(timedelta(hours=8))


class ChinaTimeFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, CHINA_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec='seconds')

WS_URL             = os.getenv('WS_URL', '')
AGENT_ID           = os.getenv('AGENT_ID', socket.gethostname())
AGENT_KEY          = os.getenv('AGENT_KEY', '')
RECONNECT_DELAY    = int(os.getenv('RECONNECT_DELAY', '5'))
HEARTBEAT_INTERVAL = int(os.getenv('HEARTBEAT_INTERVAL', '30'))
STATUS_REPORT_INTERVAL = int(os.getenv('STATUS_REPORT_INTERVAL', '120'))
OUTBOX_PATH        = os.getenv('OUTBOX_PATH', 'outbox.json')
HEALTH_HOST        = os.getenv('HEALTH_HOST', '0.0.0.0')
HEALTH_PORT        = int(os.getenv('HEALTH_PORT', '18081'))
# agent 自己也是容器，跟被管的业务容器各在独立的 bridge 网络命名空间，127.0.0.1 谁也连不到谁；
# host.docker.internal 需要 compose 里配 extra_hosts: host-gateway 才解析得到，见 docker-compose.yml
APP_HOST           = os.getenv('APP_HOST', 'host.docker.internal')
# admin(worker) 调本机 agent /queryPlugin 的共享 secret（odk init 每机随机生成、渲染进本 .env 与 admin 请求头）。
# 防同网段其它主机裸调该端点；非空时强制校验。
AGENT_LOCAL_SECRET = os.getenv('AGENT_LOCAL_SECRET', '')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
for handler in logging.getLogger().handlers:
    handler.setFormatter(ChinaTimeFormatter('%(asctime)s - %(levelname)s - %(message)s'))

if not WS_URL:
    sys.exit("ERROR: WS_URL is not set. Example: ws://192.168.1.100:8080/ws/agent")
if not AGENT_KEY:
    sys.exit("ERROR: AGENT_KEY is not set.")
