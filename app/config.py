import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8080"))
    auth_token: str = os.getenv("AUTH_TOKEN", "change-me")
    heartbeat_timeout: int = int(os.getenv("HEARTBEAT_TIMEOUT", "90"))
    command_history_limit: int = int(os.getenv("COMMAND_HISTORY_LIMIT", "200"))


settings = Settings()
