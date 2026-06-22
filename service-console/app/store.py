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
from typing import Any, Callable, Sequence, TypeVar

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import storage, tokens
from app.db_models import (
    FetchRecord,
    Namespace,
    Plugin,
    PluginAttachment,
    PluginVersion,
    Service,
    ServicePlugin,
    ServicePluginVersion,
)
# 发现上报落地表(P3-4)模型在 hub 命名空间;合并后与 console 共用同一 DB / session_factory,
# 故节点寻址(P3-6「发现权威」)直接经本 store 的 _db() 查询,无需另立 hub 侧查询入口。
from app.hub.db_models import DiscoveredNodeModel


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


def list_discovered_nodes(
    agent_id: str,
    nacos_service: str | None = None,
    status: str | None = "active",
) -> list[DiscoveredNodeModel]:
    """查某 agent(可再按 nacosService)名下 agent 自动发现上报的 DiscoveredNode 行(P3-6「发现权威」)。

    节点操作的寻址权威源是 agent 周期发现上报、已落库的 `discovered_nodes`(承载 dir / image /
    containerId / composeProject 的**真值**),而非手配的 `Service.dir/default_image`(后者退化为
    迁移期回退)。本查询供寻址解析(及将来实例页)按 (agentId[, nacosService]) 取候选实例。

    - `agent_id`:必填,= namespace.code(= agent 注册标识)。
    - `nacos_service`:可选,按 `nacos_service` 列再过滤(寻址按 Service.nacos_service_name 收敛实例)。
    - `status`:默认 `"active"`(只取本轮上报命中的活跃行,排除缺席被标 stale 的旧行);传 `None`
      则不按 status 过滤(取全集,含 stale)。

    返回 ORM 行列表(按 id 升序,稳定顺序)。一个 nacosService 名下有多行(不同 composeProject /
    dir,如 admin / 2admin 两 compose 工程)是**正常的多实例**,由调用方按 composeProject 定位,
    不在此处去歧义。
    """
    filters: list[Any] = [DiscoveredNodeModel.agent_id == agent_id]
    if nacos_service is not None:
        filters.append(DiscoveredNodeModel.nacos_service == nacos_service)
    if status is not None:
        filters.append(DiscoveredNodeModel.status == status)
    with _db().session_factory() as session:
        stmt = select(DiscoveredNodeModel).where(*filters).order_by(DiscoveredNodeModel.id.asc())
        return list(session.execute(stmt).scalars().all())


def list_discovered_nodes_for_agents(
    agent_ids: Sequence[str], status: str | None = "active"
) -> list[DiscoveredNodeModel]:
    """**批量**取一组 agent 名下的 DiscoveredNode 行(节点列表页展示「发现权威」用,避免逐行 N+1)。

    节点列表每行已按行做 list_instances fan-out;若再逐行查 DiscoveredNode 会叠加 N 次 DB 往返。
    本函数一次性按 `agent_id IN (...)` 取齐当页所有 agent 的发现行,路由层在内存按
    (agentId, nacosService) 分组后用于覆盖 dir/image 展示值。`agent_ids` 为空 → 直接返回 `[]`
    (不发空 IN 查询)。`status` 默认 `"active"`,传 `None` 不按 status 过滤。
    """
    if not agent_ids:
        return []
    filters: list[Any] = [DiscoveredNodeModel.agent_id.in_(list(agent_ids))]
    if status is not None:
        filters.append(DiscoveredNodeModel.status == status)
    with _db().session_factory() as session:
        stmt = select(DiscoveredNodeModel).where(*filters).order_by(DiscoveredNodeModel.id.asc())
        return list(session.execute(stmt).scalars().all())


def aggregate_discovered_by_nacos(
    status: str | None = "active",
) -> dict[str, list[DiscoveredNodeModel]]:
    """跨机聚合(P4-0):按 `nacos_service` 把发现实例分组 → `{nacos_service: [DiscoveredNodeModel...]}`。

    一个「跨多台机」的逻辑服务 = **同 `nacos_service` 在多个 agent 上各有实例**(ns ↔ agent 1:1,
    跨机即跨 agent;见设计 §4.1 评审 M12)。本函数把所有 agent 的 active 发现行按 nacos 名聚到一起,
    供服务对账(P3-7,意图 `Service` ⋈ 现实 `DiscoveredNode` by nacosServiceName)与将来 P4-1
    跨 agent 顺序投放协调器复用。

    - `status`:默认 `"active"`(只聚本轮上报命中的活跃实例,排除缺席被标 stale 的旧行);传 `None`
      则不按 status 过滤(取全集,含 stale)。
    - **`nacos_service` 为 None 的行跳过**——未匹配 nacos 的容器(docker 发现到但 nacos 无注册)无法
      按服务对账,不参与分组(否则会聚成一个含义不明的 None 组)。

    分组内各 list 按 id 升序(稳定顺序);同一 nacos 名跨多 agent 的实例会落进同一 list(评审 H-5
    「同 nacosService 跨多 agent 聚合」)。
    """
    filters: list[Any] = []
    if status is not None:
        filters.append(DiscoveredNodeModel.status == status)
    grouped: dict[str, list[DiscoveredNodeModel]] = {}
    with _db().session_factory() as session:
        stmt = select(DiscoveredNodeModel).where(*filters).order_by(DiscoveredNodeModel.id.asc())
        for dn in session.execute(stmt).scalars().all():
            if dn.nacos_service is None:  # 未匹配 nacos 的容器不参与按服务对账(跳过)
                continue
            grouped.setdefault(dn.nacos_service, []).append(dn)
    return grouped


def list_services_with_namespace_code() -> list[dict[str, Any]]:
    """取**全部** Service 行 + LEFT JOIN 回 `namespace_code`(服务对账 P3-7 用,不分页)。

    对账实时计算意图侧(`Service`)⋈ 现实侧(`DiscoveredNode`),数据量 = 服务数(通常不大),故
    一次取全集而非分页(避免逐页拼装)。每行 dict 含 `service_code` / `nacos_service_name` /
    `namespace_code`(namespace 缺失时为 None,LEFT JOIN)。复用 `list_rows_joined` 的列/JOIN 范式,
    但这里取全量(不传 page/page_size 上的硬上限,服务数小)。
    """
    with _db().session_factory() as session:
        stmt = (
            select(
                Service.service_code,
                Service.nacos_service_name,
                Namespace.code.label("namespace_code"),
            )
            .outerjoin(Namespace, Namespace.id == Service.namespace_id)
            .order_by(Service.id.asc())
        )
        return [dict(m) for m in session.execute(stmt).mappings().all()]


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


def create_version_with_attachment(
    *,
    plugin_id: int,
    version: str,
    name: str | None,
    filename: str,
    size: int,
    store_file: Callable[[int], str],
) -> tuple[PluginVersion, PluginAttachment]:
    """**原子**落地一个插件版本 + 其附件(评审 B2 + A6)。

    单事务内:① add `PluginVersion` → `flush()` 拿到 version_id(落盘路径需要它)→
    ② 回调 `store_file(version_id)` 落盘 .tgz 拿 `storage_path` → ③ add `PluginAttachment`
    (引用该 storage_path)→ ④ commit。**version + attachment 同生共死**——任一步异常:
    整事务 rollback(version 与 attachment 都不留)+ 删除**已落盘文件**(`storage.safe_remove`,
    realpath 校验在根内)+ 重抛。

    去重:重复 (plugin_id, version) 撞 UNIQUE → `IntegrityError` → `Conflict`(路由层 → 409),
    且**回滚后盘上无残留文件**(若落盘已发生)。

    解决旧三步三事务(version→store_tgz→attachment 各独立 commit)的「有 version 无
    attachment」半成品:撞 UNIQUE 卡重传 + query INNER JOIN 天然剔除(前端见 version 却不在
    分发清单)。`store_file` 回调把落盘夹在 flush 与 attachment 之间,落盘失败同样整体回滚。
    """
    now = _now()
    stored_path: str | None = None
    with _db().session_factory() as session:
        try:
            pv = PluginVersion(
                plugin_id=plugin_id,
                version=version,
                name=name,
                created_at=now,
                updated_at=now,
            )
            session.add(pv)
            session.flush()  # 拿到 pv.id(落盘路径段需要),仍在同一未提交事务内

            stored_path = store_file(pv.id)  # 落盘 .tgz,返回入库相对路径

            att = PluginAttachment(
                plugin_version_id=pv.id,
                filename=filename,
                size=size,
                storage_path=stored_path,
                created_at=now,
            )
            session.add(att)
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if stored_path:  # 回滚后清掉已落盘文件,避免孤儿(A6)
                storage.safe_remove(stored_path)
            raise Conflict(str(exc.orig)) from exc
        except Exception:
            session.rollback()
            if stored_path:
                storage.safe_remove(stored_path)
            raise
        session.refresh(pv)
        session.refresh(att)
        return pv, att


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

    绑定不存在 / 版本不归属本插件 → `NotFound`;并发置活撞 UNIQUE → `Conflict`(路由层 → 409)。
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

        # 版本归属校验(最终评审修复):plugin_version_id 必须存在且归属 plugin_id,
        # 否则可构造跨插件错配写进台账。沿用 NotFound(路由层 → 404),与 reactivate/rollback 一致。
        pv = session.get(PluginVersion, plugin_version_id)
        if pv is None or pv.plugin_id != plugin_id:
            raise NotFound("plugin version not found for this plugin")

        # 评审 B1(重复版本守卫,用户定 D1①):同绑定已发过该 plugin_version_id → Conflict(路由层 → 409),
        # 不再静默追加重复历史行。在父锁内做(已 with_for_update 锁 service_plugin),先于全灭活/INSERT,
        # 避免重发自灭活自身后又追加重复行。重新激活历史版本走 reactivate(不经此路径)。
        #
        # 复审 Minor-2:守卫作用域用 (service_id, plugin_id, plugin_version_id),**不用 sp.id**。
        # MySQL 下「删建同 (service_id,plugin_id) 绑定」后 service_plugin.id 会变化 → 按 sp.id 查不到
        # rebind 前的旧历史行 → 漏判而追加重复历史行。改为与单活键 spv_active_key=f"{service_id}-{plugin_id}"
        # 同一作用域(service_id+plugin_id)判重,跨 rebind 仍能命中旧行。
        dup = session.execute(
            select(ServicePluginVersion.id).where(
                ServicePluginVersion.service_id == service_id,
                ServicePluginVersion.plugin_id == plugin_id,
                ServicePluginVersion.plugin_version_id == plugin_version_id,
            )
        ).first()
        if dup is not None:
            raise Conflict("该版本已发布过(同绑定重复发布同一版本)")

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
            # 复审 Minor-1:并发置活撞 spv_active_key UNIQUE 的兜底路径。路由层已改为 str(exc) 透传,
            # 故这里须给可读中文(不能漏 str(IntegrityError) 的英文 SQL 到前端)。
            raise Conflict("并发发布冲突,请重试") from exc
        session.refresh(record)
        return record


def reactivate(spv_id: int) -> ServicePluginVersion:
    """历史重新激活:把指定历史版本行置为唯一 active(M-6:同时清 is_rolled_back=False)。

    spv 不存在 → `NotFound`;并发置活撞 UNIQUE → `Conflict`(路由层 → 409)。

    ⚠️ 评审 C2(并发正确性)+ R1:**先锁同绑定 service_plugin 父行,锁后再对目标 spv 行
    locking read 重取最新已提交状态(`populate_existing=True` 强制刷新,绕开 identity-map
    缓存)**,杜绝 MySQL RR 下基于锁前陈旧快照决策。锁前的 `session.get` 仅用于定位父行 id
    (不参与任何状态决策)。
    """
    now = _now()
    with _db().session_factory() as session:
        # 锁前先无锁定位父行 id(仅用于拿 service_plugin_id 去锁父行,不据此做决策)。
        locator = session.get(ServicePluginVersion, spv_id)
        if locator is None:
            raise NotFound("service_plugin_version 不存在")
        service_plugin_id = locator.service_plugin_id

        # 锁同绑定的 service_plugin 行(MySQL8 真行锁;sqlite no-op,见 L1),串行化同绑定写。
        session.execute(
            select(ServicePlugin)
            .where(ServicePlugin.id == service_plugin_id)
            .with_for_update()
        ).scalar_one_or_none()

        # 评审 C2 + R1:持父锁后对目标 spv 行 locking read 重取最新已提交状态,后续守卫/状态机均基于此。
        # **必须 populate_existing=True**:锁前的 locator 已把该行装进 identity map,不加此参数时
        # session.get 会命中缓存直接返回**陈旧对象**(不发 SQL、不刷属性),守卫/候选仍消费锁前快照
        # (复审 R1:实测 again is loc);populate_existing 强制覆盖缓存属性,真正读到锁后最新状态。
        target = session.get(
            ServicePluginVersion, spv_id, with_for_update=True, populate_existing=True
        )
        if target is None:  # 极端:定位后被并发删除
            raise NotFound("service_plugin_version 不存在")

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
        # 锁前先无锁定位父行 id(仅用于拿 service_plugin_id 去锁父行,不据此做决策)。
        locator = session.get(ServicePluginVersion, spv_id)
        if locator is None:
            raise NotFound("service_plugin_version 不存在")
        service_plugin_id = locator.service_plugin_id

        # 评审 C2(并发正确性):**先锁同绑定 service_plugin 父行**(MySQL8 真行锁;sqlite no-op,见 L1),
        # 串行化同绑定写,再做后续所有读+决策。
        session.execute(
            select(ServicePlugin)
            .where(ServicePlugin.id == service_plugin_id)
            .with_for_update()
        ).scalar_one_or_none()

        # 评审 C2 + R1:持父锁后对当前行 locking read 重取最新已提交状态,`is_active` 守卫基于此判定
        # (锁前快照在 RR 下可能已被并发 publish/rollback 改写 → is_rolled_back 误标/回滚链错乱)。
        # **必须 populate_existing=True**:锁前 locator 已进 identity map,不加则 get 命中缓存返回
        # 陈旧对象、守卫读到锁前快照(复审 R1);强制覆盖缓存属性方能基于锁后最新状态决策。
        current = session.get(
            ServicePluginVersion, spv_id, with_for_update=True, populate_existing=True
        )
        if current is None:  # 极端:定位后被并发删除
            raise NotFound("service_plugin_version 不存在")
        if not current.is_active:
            raise Conflict("仅能回滚当前 active 版本")

        # 候选:同绑定、order 更小、未回滚、非 active 中 order 最大者(在父锁内选,基于最新已提交状态)。
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


# --- 分发(pull token 鉴权 + id 归属式下载 + fetch_record;Task 11) ----------
#
# ⚠️ 安全不变式(本任务核心,逐条照做):
# 1. pull token 解析:**对空明文先短路返回 None**,再遍历有 hash 的 namespace 用
#    `tokens.verify_token`(常量时间)逐一比对;无匹配 → None。**绝不裸 `==` 比哈希。**
# 2. download 归属校验:只靠 token→ns + active spv 链,**不靠任何 query 参数**;不符
#    一律由路由层映射 404(防 IDOR 探测存在性,不是 403)。
# 3. fetch_record 写入走 snake 字段名(by_alias=False),勿被 camel 键灌坏多词列。


def resolve_namespace_by_pull_token(plain: str | None) -> Namespace | None:
    """按明文 pull token 反解所属 namespace(常量时间逐一比对)。

    安全不变式 #1:
    - 明文为空/None → 立即返回 None(fail-closed,绝不进入比对)。
    - 仅取 `pull_token_hash IS NOT NULL` 的 namespace,逐行 `tokens.verify_token`
      (内部 `hmac.compare_digest` 常量时间);**绝不先 `==` 裸比哈希**。
    - 无任何匹配 → None。
    """
    if not plain:  # None / 空明文先短路(verify_token 也 fail-closed,这里再加一层)
        return None
    with _db().session_factory() as session:
        rows = list(
            session.execute(
                select(Namespace).where(Namespace.pull_token_hash.is_not(None))
            )
            .scalars()
            .all()
        )
        for ns in rows:
            if tokens.verify_token(plain, ns.pull_token_hash or ""):
                return ns
        return None


def query_active_plugins(namespace_code: str, service_code: str) -> list[dict[str, Any]]:
    """查 (namespace_code, service_code) 下所有 active 版本的可拉取插件(对齐旧 queryPlugin)。

    链(镜像旧 SQL):service_plugin_version(is_active=True)→ service(service_code
    匹配,namespace_id 指向 namespace_code)→ namespace(code 匹配)→ plugin(取 code
    = pluginName)→ plugin_version(取 version,NOT NULL 恒非空)→ plugin_attachment
    (取该 version 下 id 最大的一条作为下载目标)。

    返回每命中插件一个 dict,含返回契约字段(plugin_code/version/attachment_id)+ 写
    fetch_record 所需的 namespace_id/service_id/plugin_id/plugin_version_id。attachment
    缺失的行被 INNER JOIN 天然剔除(无包可下,不应出现在分发清单)。
    """
    with _db().session_factory() as session:
        stmt = (
            select(
                Namespace.id.label("namespace_id"),
                Service.id.label("service_id"),
                Plugin.id.label("plugin_id"),
                Plugin.code.label("plugin_code"),
                PluginVersion.id.label("plugin_version_id"),
                PluginVersion.version.label("version"),
                func.max(PluginAttachment.id).label("attachment_id"),
            )
            .select_from(ServicePluginVersion)
            .join(Service, Service.id == ServicePluginVersion.service_id)
            .join(Namespace, Namespace.id == Service.namespace_id)
            .join(Plugin, Plugin.id == ServicePluginVersion.plugin_id)
            .join(PluginVersion, PluginVersion.id == ServicePluginVersion.plugin_version_id)
            .join(PluginAttachment, PluginAttachment.plugin_version_id == PluginVersion.id)
            .where(
                ServicePluginVersion.is_active.is_(True),
                Namespace.code == namespace_code,
                Service.service_code == service_code,
            )
            .group_by(
                Namespace.id,
                Service.id,
                Plugin.id,
                Plugin.code,
                PluginVersion.id,
                PluginVersion.version,
            )
            .order_by(Plugin.code.asc())
        )
        return [dict(m) for m in session.execute(stmt).mappings().all()]


def attachment_in_namespace(attachment_id: int, namespace_id: int) -> PluginAttachment | None:
    """归属式取 attachment(安全不变式 #2:防 IDOR)。

    仅当存在一条 **active** 的 service_plugin_version,把该 attachment 的
    plugin_version 关联到「给定 namespace」下的某个 service 时,才返回该 attachment
    (一个 version 可被多个 service 绑定,只要存在一条 active 链到本 namespace 即放行)。
    否则返回 None(路由层映射 404,不暴露存在性)。

    **只靠 attachment→plugin_version→spv(active)→service→namespace == 给定 namespace**,
    不接受任何 query 参数旁路。
    """
    with _db().session_factory() as session:
        att = session.get(PluginAttachment, attachment_id)
        if att is None:
            return None
        linked = session.execute(
            select(ServicePluginVersion.id)
            .select_from(ServicePluginVersion)
            .join(Service, Service.id == ServicePluginVersion.service_id)
            .where(
                ServicePluginVersion.plugin_version_id == att.plugin_version_id,
                ServicePluginVersion.is_active.is_(True),
                Service.namespace_id == namespace_id,
            )
            .limit(1)
        ).first()
        return att if linked is not None else None


def create_fetch_record(
    *,
    namespace_id: int,
    service_id: int,
    plugin_id: int,
    plugin_version_id: int,
    remark: str | None = None,
) -> FetchRecord:
    """写一行拉取审计记录(fetch_date 服务端填当前 UTC)。

    全部 snake 字段名(安全不变式 #3:勿用 camel 键写 ORM,会灌坏 plugin_version_id 等多词列)。
    """
    now = _now()
    return create_row(
        FetchRecord,
        {
            "namespace_id": namespace_id,
            "service_id": service_id,
            "plugin_id": plugin_id,
            "plugin_version_id": plugin_version_id,
            "fetch_date": now,
            "remark": remark,
        },
    )
