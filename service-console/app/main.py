from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db import Database
from app.middleware import (
    SecurityHeadersMiddleware,
    SessionGuardMiddleware,
    mount_spa,
)
from app.routers.auth import router as auth_router
from app.routers.distribution import router as distribution_router
from app.routers.fetch_records import router as fetch_records_router
from app.routers.namespaces import router as namespaces_router
from app.routers.nodes import operations_router as node_operations_router
from app.routers.nodes import router as nodes_router
from app.routers.plugin_versions import router as plugin_versions_router
from app.routers.plugins import router as plugins_router
from app.routers.releases import router as releases_router
from app.routers.service_plugins import router as service_plugins_router
from app.routers.services import router as services_router
from app.routers.system import router as system_router

# ── 并入的 hub 模块(S2;原 service-hub,控制链:agent-WS / 命令 / 滚动 / 日志)──
# _handle_agent_message / _remote_address 在此 import 即成为 app.main 模块全局,
# 供 hub 路由 / WS 经 `import app.main as main_module; main_module.X` 取用。
from app.hub.api_support import _handle_agent_message, _remote_address  # noqa: F401 (经 main_module 取用)
from app.hub.config import settings as hub_settings
from app.hub.db import Database as HubDatabase
from app.hub.store import HubState
from app.hub.routers.agent_ws import router as agent_ws_router
from app.hub.routers.agents import router as agents_router
from app.hub.routers.commands import router as commands_router
from app.hub.routers.logs import router as logs_router
from app.hub.routers.rolling import router as rolling_router


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 评审 M10/L2:database 单例唯一落点(store / routers 一律函数内延迟 `import app.main as main_module`
# 后取 `main_module.database`,禁止模块级 `from app.main import database`),不在 app/db.py 建。
database = Database(settings.database_url)

# 并入的 hub 单例(S2):hub_state 落点在此(hub 路由 / WS 经 main_module.hub_state 取用)。
# hub_database 过渡期独立(settings.hub_database_url,避免与 console 库共用 DATABASE_URL 撞 _managed_tables
# 守卫);S4 合 DB 后并入 database。
hub_database = HubDatabase(settings.hub_database_url)
hub_state = HubState(
    heartbeat_timeout=hub_settings.heartbeat_timeout,
    command_history_limit=hub_settings.command_history_limit,
    database=hub_database,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 评审(被否决条目残留 hardening):空 / 过短 jwt_secret 拒绝启动(纵深防御,配合 Task 3 pin PyJWT)。
    if not settings.jwt_secret or len(settings.jwt_secret) < 32:
        raise RuntimeError("PLATFORM_JWT_SECRET 未配置或过短(须 ≥32 字符)")
    database.init_schema()
    # 并入的 hub(S2):建 hub 表 + 恢复中断的滚动任务。**务必保留 interrupt_running_rolling**,
    # 否则重启后中断的滚动永不被标 interrupted(评审 M-9)。
    hub_database.init_schema()
    await hub_state.initialize()
    interrupted = await hub_state.interrupt_running_rolling()
    if interrupted:
        logger.warning("启动恢复:发现 %s 个中断的滚动任务,已标记 interrupted", interrupted)
    yield


# 评审 A15/B3:docs/redoc/openapi 不在 /api 前缀下,SessionGuardMiddleware 的 default-deny
# 不覆盖它们,开着即匿名暴露全 API 面。默认 enable_docs=false(生产安全)→ 三者全 None(404);
# 仅当 PLATFORM_ENABLE_DOCS 显式打开时才挂内置文档(本机调试用)。
_docs_kwargs = (
    {}
    if settings.enable_docs
    else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)
app = FastAPI(title="service-console", version="0.1.0", lifespan=lifespan, **_docs_kwargs)

# ── 中间件注册 ───────────────────────────────────────────────────────────────────
# ⚠️ add_middleware **逆序**生效(最后 add 的在**最外层**先跑)。期望的链(外→内):
#     SecurityHeaders → SessionGuard → SPAFallback → router
#   - SecurityHeaders 最外层:给**所有**响应(含 SessionGuard 的 401、SPA 静态、异常)注入
#     CSP/安全头,无遗漏 → 必须**最后** add。
#   - SPAFallback 最内层(贴着 router):只在 router 返回 404 后兜底回前端产物,API 路由
#     (含运行期新增的 /api 路由)永远优先,绝不吞 API → 必须**最先** add。
#   - SessionGuard 居中:对 /api/**(白名单外)default-deny 校验 Bearer JWT。
# 故 add 顺序 = 期望链的**逆序**:SPAFallback → SessionGuard → SecurityHeaders。
#
# SPAFallback 仅当 app/static 存在(有前端构建产物)时启用;纯后端/测试环境(无产物)跳过。
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if mount_spa(app, _static_dir):
    logger.info("SPA 静态资源已启用(fallback 托管):%s", _static_dir)
else:
    logger.info("未发现 app/static(无前端构建产物),跳过 SPA 托管(纯后端模式)")
app.add_middleware(SessionGuardMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

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
app.include_router(nodes_router)
app.include_router(node_operations_router)  # Task 10b:/api/node-operations(与 /api/nodes 平级)
# 并入的 hub 路由(S2):hub 自带 admin-token 自校验,已在 SessionGuard 白名单放行(见 middleware)。
# 不含 hub system 路由(console 已有自己的 /health)。
app.include_router(agents_router)
app.include_router(commands_router)
app.include_router(logs_router)
app.include_router(rolling_router)
app.include_router(agent_ws_router)
