import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8080"))
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./service-console.db")
    admin_user: str = os.getenv("PLATFORM_ADMIN_USER", "")
    admin_password: str = os.getenv("PLATFORM_ADMIN_PASSWORD", "")
    jwt_secret: str = os.getenv("PLATFORM_JWT_SECRET", "")
    jwt_ttl_seconds: int = int(os.getenv("PLATFORM_JWT_TTL", "28800"))  # 8h
    # 评审 A15/B3:/openapi.json /docs /redoc 不在 /api 前缀下,default-deny 中间件不覆盖,
    # 开着即匿名暴露全 API 面。默认 false(生产安全);本机调试可设 PLATFORM_ENABLE_DOCS=true。
    enable_docs: bool = os.getenv("PLATFORM_ENABLE_DOCS", "false").strip().lower() in ("1", "true", "yes", "on")
    service_hub_url: str = os.getenv("SERVICE_HUB_URL", "")
    hub_admin_token: str = os.getenv("HUB_ADMIN_TOKEN", "")
    plugin_storage_dir: str = os.getenv("PLUGIN_STORAGE_DIR", "./data/plugins")
    plugin_download_base_url: str = os.getenv("PLUGIN_DOWNLOAD_BASE_URL", "")
    # ── 并入的 hub 配置(S3;原 service-hub/app/config.py)──
    # admin_token:hub 控制链路由(/api/agents 等)的 X-Admin-Token 自校验(与平台 JWT 正交)。
    admin_token: str = os.getenv("ADMIN_TOKEN", "")
    heartbeat_timeout: int = int(os.getenv("HEARTBEAT_TIMEOUT", "90"))
    command_history_limit: int = int(os.getenv("COMMAND_HISTORY_LIMIT", "200"))
    rolling_settle_sec: int = int(os.getenv("ROLLING_SETTLE_SEC", "35"))
    rolling_shutdown_timeout: int = int(os.getenv("ROLLING_SHUTDOWN_TIMEOUT", "60"))
    rolling_ready_timeout: int = int(os.getenv("ROLLING_READY_TIMEOUT", "180"))
    rolling_cmd_timeout: int = int(os.getenv("ROLLING_CMD_TIMEOUT", "480"))
    list_instances_timeout: int = int(os.getenv("LIST_INSTANCES_TIMEOUT", "10"))
    force_op_max_per_window: int = int(os.getenv("FORCE_OP_MAX_PER_WINDOW", "10"))
    force_op_window_sec: int = int(os.getenv("FORCE_OP_WINDOW_SEC", "60"))


settings = Settings()
