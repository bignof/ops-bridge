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

# --- 插件分发(P1):agent 本机插件缓存 + worker-facing 端点 + 回源平台 ---
# 均为可选能力(未配则该 agent 不提供插件分发,不 sys.exit)。
PLATFORM_URL           = os.getenv('PLATFORM_URL', '')                 # 回源平台基址,如 https://console.example.com(末尾不带 /)
PULL_TOKEN             = os.getenv('PULL_TOKEN', '')                   # 本 namespace 的 pull-token(回源 Bearer)
PLUGIN_NAMESPACE       = os.getenv('PLUGIN_NAMESPACE', '')            # agent 自身的本 namespace(回源用);worker 传入的 namespace 一律忽略
PLUGIN_CACHE_DIR       = os.getenv('PLUGIN_CACHE_DIR', '/data/agent-plugin-cache')   # 缓存 .tgz 落盘目录(可挂卷保留)
PLUGIN_CACHE_MAX_BYTES = int(os.getenv('PLUGIN_CACHE_MAX_BYTES', str(2 * 1024 * 1024 * 1024)))  # 容量上限,LRU 淘汰;<=0=不限制
# worker-facing HTTP:默认仅本机 127.0.0.1(不复用 HEALTH_HOST 的 0.0.0.0;worker 同主机访问,不出主机)。
PLUGIN_SERVE_HOST      = os.getenv('PLUGIN_SERVE_HOST', '127.0.0.1')
PLUGIN_SERVE_PORT      = int(os.getenv('PLUGIN_SERVE_PORT', '18082'))

# --- 自动发现上报(P3):周期把本机 compose 容器(含 stopped)+ nacos 实例经 WS 报给 console ---
DISCOVERY_INTERVAL     = int(os.getenv('DISCOVERY_INTERVAL', '30'))  # 秒;<=0 = 禁用发现上报线程

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
