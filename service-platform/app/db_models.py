"""SQLAlchemy ORM 模型(8 张台账表)。

Task 1 仅放占位:`migrations/env.py` 照搬 service-hub 范式时会
`from app import db_models` 以收集 `Base.metadata`(autogenerate / target_metadata)。
为保证骨架阶段 `init_schema()` 调 alembic 不因缺模块而 ImportError,本文件须随
Task 1 一并存在。

8 张表的实际模型(namespace / service / plugin / plugin_version /
plugin_attachment / service_plugin / service_plugin_version / fetch_record)
由后续 Task 填充;届时 `Base.metadata` 自动纳入新模型,无需改 env.py。
"""

from app.db import Base  # noqa: F401
