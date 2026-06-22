"""测试地基(评审 B2)。

布局对齐 `service-hub/tests/`:
- 文件顶部**只**做 `sys.path` 注入(与 `service-hub/tests/conftest.py` 字面一致),
  保证 `import app.*` 在 cwd=service-platform 下可解析。
- 真正的 DB 隔离靠下方 `client` fixture:每个用例独立 `tmp_path` 文件库 +
  `init_schema()` + swap `app.main.database` 单例 + `object.__setattr__` 改 frozen
  settings + 退出 `dispose()` 并还原。**禁用 `sqlite:///:memory:` 单例**(跨用例
  状态泄漏)。
- `client` fixture 放此处(conftest)以便后续所有 Task 的端点测试与直调 `store.*`
  的测试零摩擦复用(pytest 自动注入);这是 Task 1 的关键交付与验收门。

注:`os.environ.setdefault` 之所以放在本文件(而非 service-hub 那样放 test_api.py
顶部),是因为本平台把可复用的 `client` fixture 收敛到 conftest;config 在
`import app.config` 时即读 env,故须在 import 之前做一次进程级兜底
(照 service-hub test_api.py 顶部 setdefault 的做法)。
"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
root_str = str(ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)


import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

# config 在 import app.config 时读 env,故进程级兜底一次(照 service-hub test_api.py 顶部 setdefault)。
# jwt_secret 须 ≥32 字符,否则 app.main 的 lifespan 会拒绝启动(纵深防御)。
os.environ.setdefault("PLATFORM_ADMIN_USER", "admin")
os.environ.setdefault("PLATFORM_ADMIN_PASSWORD", "admin-pw")
os.environ.setdefault("PLATFORM_JWT_SECRET", "test-secret-which-is-long-enough-0123456789")

from app.db import Database  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    """每个用例独立的临时文件库 + 隔离的 FastAPI 测试客户端。

    后续所有 Task 的端点测试与直调 `store.*` 的测试一律经此 fixture 注入
    临时文件库——禁止裸 `TestClient(app)` 或 `:memory:` 单例(评审 B2)。

    S5:hub 已并入本进程,platform→hub 改进程内直调 `app.hub_client`(经
    `main_module.hub_state`)。故除 swap `main_module.database` 外,还须把
    `hub_state.database` 一并指向同一临时库 —— 否则未打桩的进程内 hub 调用会落到模块
    加载期的旧库(BFF 端点测试普遍打桩 `hub_client.*`,触不到;但 `test_hub_client.py`
    的进程内适配器测试会真跑 hub 逻辑,必须共用同一隔离库)。同时给 `admin_token` 一个
    测试值,满足 hub handler 首行 `_require_admin_token` 自校验(进程内适配器传它)。
    """
    database = Database("sqlite:///" + str(tmp_path / "test.db"))
    database.init_schema()

    import app.main as main_module

    old_database = main_module.database
    main_module.database = database  # swap 单例(store/routers 函数内取 main_module.database)
    # hub_state 在模块加载期已用旧库构造,这里把它的 database 一并切到临时库(进程内 hub 调用据此读写)。
    old_hub_db = main_module.hub_state.database
    main_module.hub_state.database = database
    # frozen settings 字段一律用 object.__setattr__ 改;给 admin_token 测试值(进程内 handler 自校验需非空)。
    old_admin_token = main_module.settings.admin_token
    object.__setattr__(main_module.settings, "admin_token", "test-admin-token")
    app.dependency_overrides = {}

    with TestClient(app) as test_client:
        yield test_client

    database.engine.dispose()
    main_module.database = old_database  # 还原,防跨用例泄漏
    main_module.hub_state.database = old_hub_db
    object.__setattr__(main_module.settings, "admin_token", old_admin_token)
