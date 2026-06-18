import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8080"))
    admin_token: str = os.getenv("ADMIN_TOKEN", "")
    heartbeat_timeout: int = int(os.getenv("HEARTBEAT_TIMEOUT", "90"))
    command_history_limit: int = int(os.getenv("COMMAND_HISTORY_LIMIT", "200"))
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./service-hub.db")
    rolling_settle_sec: int = int(os.getenv("ROLLING_SETTLE_SEC", "35"))
    rolling_shutdown_timeout: int = int(os.getenv("ROLLING_SHUTDOWN_TIMEOUT", "60"))
    rolling_ready_timeout: int = int(os.getenv("ROLLING_READY_TIMEOUT", "180"))
    rolling_cmd_timeout: int = int(os.getenv("ROLLING_CMD_TIMEOUT", "480"))  # 须 ≥ shutdown60+restart120+ready180+settle35=395 + 余量


settings = Settings()
