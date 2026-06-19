"""泛型台账 CRUD helper(全计划共用)。

绑定约束:
- **database 单例(评审 M10/L2)**:单例唯一落点是 `app/main.py`。本模块**一律函数内
  延迟** `import app.main as main_module` 取 `main_module.database`(见 `_db()`),
  **禁止模块级** `from app.main import database`——它是真循环 import,且 import 期
  绑定会让 `client` fixture 的 per-test 换库失效。
- **唯一冲突 → 409**:`create_row` / `update_row` 捕 `IntegrityError` 抛自定义
  `Conflict`,路由层映射成 HTTP 409。
- 写操作的鉴权由路由的 `require_session` 依赖 + default-deny 中间件负责,本层不管。

返回值一律是 ORM 行(snake 属性);路由层经 `*Out` 模型转 camelCase(评审 H2)。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence, TypeVar

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError


ModelT = TypeVar("ModelT")


class Conflict(Exception):
    """唯一约束冲突(create/update 触发);路由层映射为 HTTP 409。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    # 延迟取单例:避免循环 import + 使 `client` fixture 的 per-test 换库生效(评审 M10)。
    import app.main as main_module

    return main_module.database


def list_rows(
    model: type[ModelT],
    *,
    page: int = 1,
    page_size: int = 20,
    filters: Sequence[Any] | None = None,
    order_by: Any | None = None,
) -> tuple[list[ModelT], int]:
    """分页查询:返回 `(rows, count)`。`filters` 为 SQLAlchemy 条件表达式序列。"""
    page = max(1, page)
    page_size = max(1, page_size)
    with _db().session_factory() as session:
        base = select(model)
        count_stmt = select(func.count()).select_from(model)
        for cond in filters or []:
            base = base.where(cond)
            count_stmt = count_stmt.where(cond)
        count = int(session.execute(count_stmt).scalar_one())
        base = base.order_by(order_by if order_by is not None else getattr(model, "id").asc())
        rows = session.execute(base.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        return list(rows), count


def get_row(model: type[ModelT], row_id: int) -> ModelT | None:
    with _db().session_factory() as session:
        return session.get(model, row_id)


def create_row(model: type[ModelT], values: dict[str, Any]) -> ModelT:
    """插入一行;`created_at`/`updated_at` 若模型有该列且未显式传入则自动补当前 UTC。

    捕 `IntegrityError`(唯一约束等)→ 抛 `Conflict`(路由层 → 409)。
    """
    now = _now()
    payload = dict(values)
    for ts_field in ("created_at", "updated_at"):
        if hasattr(model, ts_field) and ts_field not in payload:
            payload[ts_field] = now
    with _db().session_factory() as session:
        record = model(**payload)
        session.add(record)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise Conflict(str(exc.orig)) from exc
        session.refresh(record)
        return record


def update_row(model: type[ModelT], row_id: int, values: dict[str, Any]) -> ModelT | None:
    """局部更新已存在行(只覆盖 `values` 中的键);行不存在返回 None。

    捕 `IntegrityError` → 抛 `Conflict`(路由层 → 409)。
    """
    now = _now()
    with _db().session_factory() as session:
        record = session.get(model, row_id)
        if record is None:
            return None
        for key, value in values.items():
            setattr(record, key, value)
        if hasattr(model, "updated_at") and "updated_at" not in values:
            record.updated_at = now
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise Conflict(str(exc.orig)) from exc
        session.refresh(record)
        return record


def delete_row(model: type[ModelT], row_id: int) -> bool:
    """删除一行;删除成功返回 True,行不存在返回 False。"""
    with _db().session_factory() as session:
        record = session.get(model, row_id)
        if record is None:
            return False
        session.delete(record)
        session.commit()
        return True
