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

from app.db_models import ServicePlugin, ServicePluginVersion


ModelT = TypeVar("ModelT")


class Conflict(Exception):
    """唯一约束冲突(create/update 触发);路由层映射为 HTTP 409。"""


class NotFound(Exception):
    """目标行不存在(发布未绑定 / spv 不存在 / 无可回滚候选);路由层映射为 HTTP 404。"""


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


def list_rows_joined(
    base_model: type[Any],
    *,
    columns: Sequence[Any],
    outer_joins: Sequence[tuple[Any, Any]] = (),
    page: int = 1,
    page_size: int = 20,
    filters: Sequence[Any] | None = None,
    order_by: Any | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """带 LEFT JOIN 的分页查询(评审 H3 回可读名 / 评审 M3 级联过滤)。

    - `columns`:要 SELECT 的列表达式序列(本行字段 + 关联表回名列,用 `.label('xxxCode')`
      命名,结果以该名进 dict)。
    - `outer_joins`:`(target_model, onclause)` 序列,逐个 `outerjoin`(LEFT JOIN,
      关联缺失时回名列为 NULL,不丢主行)。
    - `filters`:SQLAlchemy 条件表达式序列(含级联过滤的等值条件)。

    返回 `(rows, count)`,`rows` 是 `dict`(键 = column 的 label/键名),路由层经 `*Out`
    模型 `model_validate(...)` 转 camelCase。
    """
    page = max(1, page)
    page_size = max(1, page_size)
    with _db().session_factory() as session:
        base = select(*columns)
        count_stmt = select(func.count()).select_from(base_model)
        for target, onclause in outer_joins:
            base = base.outerjoin(target, onclause)
        for cond in filters or []:
            base = base.where(cond)
            count_stmt = count_stmt.where(cond)
        count = int(session.execute(count_stmt).scalar_one())
        base = base.order_by(order_by if order_by is not None else getattr(base_model, "id").asc())
        result = session.execute(base.offset((page - 1) * page_size).limit(page_size))
        rows = [dict(m) for m in result.mappings().all()]
        return rows, count


def get_row(model: type[ModelT], row_id: int) -> ModelT | None:
    with _db().session_factory() as session:
        return session.get(model, row_id)


def find_rows(model: type[ModelT], *, filters: Sequence[Any], limit: int | None = None) -> list[ModelT]:
    """按条件取匹配行(不分页);用于 plugin 匹配(精确/LIKE 命中判 0/1/多)与去重预检。

    `limit` 可选(如只需判「是否有 >1 命中」时取 2 即足)。返回 ORM 行列表。
    """
    with _db().session_factory() as session:
        stmt = select(model)
        for cond in filters:
            stmt = stmt.where(cond)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(session.execute(stmt).scalars().all())


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


# --- 发布/历史激活/回滚:单活 + 事务锁 + 状态机(Task 10) --------------------
#
# 单活不变式:每 (service,plugin) 至多一行 is_active=True;由 `spv_active_key`
# (nullable unique;active 时 = f"{service_id}-{plugin_id}",否则 None)+ DB UNIQUE 兜底。
#
# ⚠️ 评审 M4(最易踩、最常见回滚路径必崩):三函数统一「先把同绑定全部灭活 + 清 key」
#    → **`s.flush()`** → 「再置目标行 active + 设 key」。**必须 flush 分隔**:SQLAlchemy
#    在 commit 期按主键升序发 UPDATE,回滚/激活到**更低 PK 的历史行**时,低 PK 行的「置 key」
#    会先于高 PK 当前行的「清 key」发出 → UNIQUE 立即违例(sqlite/MySQL8 均即时检查)。
#    flush 把「清 key」先刷到 DB,再发「置 key」,避开该排序爆炸。
#
# 并发闸:`with_for_update()` 锁 service_plugin 行(评审 L1:sqlite 上是 no-op,MySQL8 才真
#    行锁);真正跨进程的兜底是 spv_active_key 的 UNIQUE + IntegrityError→Conflict(409)。


def _deactivate_all(session: Any, service_id: int, plugin_id: int) -> list[ServicePluginVersion]:
    """把同 (service,plugin) 下所有版本行 is_active=False + spv_active_key=None。

    返回这些行(供调用方按需取目标行)。**调用方须在置目标 key 前 `session.flush()`**
    (评审 M4),否则低 PK 历史行的置 key 会先于高 PK 当前行的清 key 发出而撞 UNIQUE。
    """
    rows = list(
        session.execute(
            select(ServicePluginVersion).where(
                ServicePluginVersion.service_id == service_id,
                ServicePluginVersion.plugin_id == plugin_id,
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.is_active = False
        row.spv_active_key = None
    return rows


def publish(service_id: int, plugin_id: int, plugin_version_id: int) -> ServicePluginVersion:
    """发布:为已绑定的 (service,plugin) 追加一版并置为唯一 active(version_order 自增)。

    绑定不存在 → `NotFound`;并发置活撞 UNIQUE → `Conflict`(路由层 → 409)。
    """
    now = _now()
    with _db().session_factory() as session:
        sp = session.execute(
            select(ServicePlugin)
            .where(ServicePlugin.service_id == service_id, ServicePlugin.plugin_id == plugin_id)
            .with_for_update()
        ).scalar_one_or_none()
        if sp is None:
            raise NotFound("service_plugin 未绑定,请先创建关联")

        # 先全灭活 + 清 key,再 flush(评审 M4),最后 INSERT 带 key 的新行。
        _deactivate_all(session, service_id, plugin_id)
        session.flush()

        max_order = session.execute(
            select(func.coalesce(func.max(ServicePluginVersion.version_order), 0)).where(
                ServicePluginVersion.service_plugin_id == sp.id
            )
        ).scalar_one()
        record = ServicePluginVersion(
            service_plugin_id=sp.id,
            service_id=service_id,
            plugin_id=plugin_id,
            plugin_version_id=plugin_version_id,
            version_order=max_order + 1,
            is_active=True,
            is_rolled_back=False,
            spv_active_key=f"{service_id}-{plugin_id}",
            publish_time=now,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise Conflict(str(exc.orig)) from exc
        session.refresh(record)
        return record


def reactivate(spv_id: int) -> ServicePluginVersion:
    """历史重新激活:把指定历史版本行置为唯一 active(M-6:同时清 is_rolled_back=False)。

    spv 不存在 → `NotFound`;并发置活撞 UNIQUE → `Conflict`(路由层 → 409)。
    """
    now = _now()
    with _db().session_factory() as session:
        target = session.get(ServicePluginVersion, spv_id)
        if target is None:
            raise NotFound("service_plugin_version 不存在")
        # 锁同绑定的 service_plugin 行(MySQL8 真行锁;sqlite no-op,见 L1)。
        session.execute(
            select(ServicePlugin)
            .where(ServicePlugin.id == target.service_plugin_id)
            .with_for_update()
        ).scalar_one_or_none()

        # 全灭活 + 清 key → flush(评审 M4)→ 置目标行 active + 设 key + 清回滚标记(M-6)。
        _deactivate_all(session, target.service_id, target.plugin_id)
        session.flush()

        target.is_active = True
        target.is_rolled_back = False
        target.spv_active_key = f"{target.service_id}-{target.plugin_id}"
        target.updated_at = now
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise Conflict(str(exc.orig)) from exc
        session.refresh(target)
        return target


def rollback(spv_id: int) -> ServicePluginVersion:
    """回滚:把当前 active 行标记 is_rolled_back=True,并激活其前一可用历史版本。

    候选谓词:同绑定下 `version_order < 当前 ∧ not is_rolled_back ∧ not is_active`
    里 version_order 最大者。返回新激活的候选行。

    spv 不存在 → `NotFound`;spv 非当前 active → `Conflict`(只能回滚当前在线版本);
    无可回滚候选 → `NotFound`;并发置活撞 UNIQUE → `Conflict`。
    """
    now = _now()
    with _db().session_factory() as session:
        current = session.get(ServicePluginVersion, spv_id)
        if current is None:
            raise NotFound("service_plugin_version 不存在")
        if not current.is_active:
            raise Conflict("仅能回滚当前 active 版本")
        session.execute(
            select(ServicePlugin)
            .where(ServicePlugin.id == current.service_plugin_id)
            .with_for_update()
        ).scalar_one_or_none()

        # 候选:同绑定、order 更小、未回滚、非 active 中 order 最大者。
        candidate = session.execute(
            select(ServicePluginVersion)
            .where(
                ServicePluginVersion.service_plugin_id == current.service_plugin_id,
                ServicePluginVersion.version_order < current.version_order,
                ServicePluginVersion.is_rolled_back.is_(False),
                ServicePluginVersion.is_active.is_(False),
            )
            .order_by(ServicePluginVersion.version_order.desc())
            .limit(1)
        ).scalar_one_or_none()
        if candidate is None:
            raise NotFound("无可回滚的历史版本")

        # 全灭活 + 清 key → flush(评审 M4:候选可能是更低 PK 历史行)→ 置候选 active + 设 key;
        # 当前行标记 is_rolled_back=True(已随 _deactivate_all 置 is_active=False/清 key)。
        _deactivate_all(session, current.service_id, current.plugin_id)
        current.is_rolled_back = True
        current.updated_at = now
        session.flush()

        candidate.is_active = True
        candidate.spv_active_key = f"{candidate.service_id}-{candidate.plugin_id}"
        candidate.updated_at = now
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise Conflict(str(exc.orig)) from exc
        session.refresh(candidate)
        return candidate
