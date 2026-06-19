from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db import Database
from app.routers.auth import router as auth_router
from app.routers.system import router as system_router


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 评审 M10/L2:database 单例唯一落点(store / routers 一律函数内延迟 `import app.main as main_module`
# 后取 `main_module.database`,禁止模块级 `from app.main import database`),不在 app/db.py 建。
database = Database(settings.database_url)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 评审(被否决条目残留 hardening):空 / 过短 jwt_secret 拒绝启动(纵深防御,配合 Task 3 pin PyJWT)。
    if not settings.jwt_secret or len(settings.jwt_secret) < 32:
        raise RuntimeError("PLATFORM_JWT_SECRET 未配置或过短(须 ≥32 字符)")
    database.init_schema()
    yield


app = FastAPI(title="service-platform", version="0.1.0", lifespan=lifespan)
# Task 3.5 起:app.add_middleware(SessionGuardMiddleware)
app.include_router(system_router)
app.include_router(auth_router)
