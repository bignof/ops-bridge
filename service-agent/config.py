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
HEALTH_HOST        = os.getenv('HEALTH_HOST', '0.0.0.0')
HEALTH_PORT        = int(os.getenv('HEALTH_PORT', '18081'))

# agent 自报版本（平台据此对旧 agent 禁用其不支持的操作，滚动升级期兼容）。
AGENT_VERSION      = os.getenv('AGENT_VERSION', '1.0.0')

# --- 镜像 registry 白名单（防供应链：pull/重部署前在 agent 这个不可绕过的执行点校验镜像来源）---
# 逗号分隔；空列表 = 不限制（放行全部），非空 = 强制。匹配语义见 services.compose.is_image_registry_allowed。
IMAGE_REGISTRY_ALLOWLIST = [x.strip() for x in os.getenv('IMAGE_REGISTRY_ALLOWLIST', '').split(',') if x.strip()]

# --- 节点控制安全闸（compose 命令目录守卫）---
MANAGED_PROJECTS_ROOT = os.getenv('MANAGED_PROJECTS_ROOT', '/data')  # 受管 compose 根目录，所有命令 dir 必须在其下
SELF_PROJECT_DIR      = os.getenv('SELF_PROJECT_DIR', '')            # agent 自身 compose 目录，禁止被操作（防自杀/越权）

# --- worker /api/k8s/shutdown 鉴权（T4a：opt-in，向后兼容）---
# 配了非空值，agent 调 worker 优雅停机端点时带 X-Shutdown-Token 头；空=不带（兼容未鉴权端点）。
# 须与 cnp 侧 /api/k8s/shutdown 校验的 token 一致。
K8S_SHUTDOWN_TOKEN    = os.getenv('K8S_SHUTDOWN_TOKEN', '')

# --- nacos（滚动重启用，可选能力，勿加 sys.exit 强校验）---
NACOS_SERVER       = os.getenv('NACOS_SERVER', '')          # 形如 192.168.0.30:8848
NACOS_NAMESPACE    = os.getenv('NACOS_NAMESPACE', '')       # 空=public
NACOS_GROUP        = os.getenv('NACOS_GROUP', 'DEFAULT_GROUP')
NACOS_CONTEXT_PATH = os.getenv('NACOS_CONTEXT_PATH', '/nacos')
NACOS_USERNAME     = os.getenv('NACOS_USERNAME', '')
NACOS_PASSWORD     = os.getenv('NACOS_PASSWORD', '')

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

# criticE：启用了受管根（MANAGED_PROJECTS_ROOT）却没设 SELF_PROJECT_DIR 时，
# handlers/log_sessions 的「拒操作 agent 自身 project」自杀防护会整段短路（默认失效）。启动时告警一次。
if MANAGED_PROJECTS_ROOT and not SELF_PROJECT_DIR:
    logging.getLogger(__name__).warning(
        "SELF_PROJECT_DIR 未设置，agent 自身 project 的自杀防护未启用；生产请设为 agent 自身 compose 目录"
    )
