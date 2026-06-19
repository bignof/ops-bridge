from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db import Database
from app.middleware import SessionGuardMiddleware
from app.routers.auth import router as auth_router
from app.routers.distribution import router as distribution_router
from app.routers.fetch_records import router as fetch_records_router
from app.routers.namespaces import router as namespaces_router
from app.routers.plugin_versions import router as plugin_versions_router
from app.routers.plugins import router as plugins_router
from app.routers.releases import router as releases_router
from app.routers.service_plugins import router as service_plugins_router
from app.routers.services import router as services_router
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
# 评审 H6 / spec L100:default-deny 守 /api/**(白名单 login/distribution/health);
# 逐路由 Depends(require_session) 保留作纵深防御(双层)。add_middleware 注册的
# 中间件按逆序执行,此处唯一中间件,故为最外层先跑。
app.add_middleware(SessionGuardMiddleware)
app.include_router(system_router)
app.include_router(auth_router)
app.include_router(plugins_router)
app.include_router(plugin_versions_router)
app.include_router(namespaces_router)
app.include_router(services_router)
app.include_router(service_plugins_router)
app.include_router(releases_router)
app.include_router(distribution_router)
app.include_router(fetch_records_router)
