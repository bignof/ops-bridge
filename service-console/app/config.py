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


settings = Settings()
