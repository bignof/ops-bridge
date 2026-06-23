"""SQLAlchemy ORM 模型(8 张台账表)。

技术栈与字段范式镜像 `service-hub/app/db_models.py`(SQLAlchemy 2.0
`Mapped`/`mapped_column`)。约束要点(评审):

- **单活不变式(M-4)**:`service_plugin_version.spv_active_key` 是 **app 维护的
  nullable unique 普通列**(active 时 = `f"{service_id}-{plugin_id}"`,否则 NULL)
  ——**禁用 MySQL 生成列**,以保证 sqlite + MySQL8 双可建。每 (service,plugin)
  至多一行 active 由该列 UNIQUE 兜底(多个 NULL 允许并存)。
- **version 不变式(M-2)**:`plugin_version.version` **NOT NULL**(= .tgz 内
  `package.json.version`)。
- 复合唯一约束:`uq_service_ns_code` / `uq_pv_plugin_version` /
  `uq_sp_service_plugin`。

DDL 真实落地见单一初始 squash 迁移 `migrations/versions/682a89c2f7d1_initial_schema_console_12_tables.py`;`init_schema()`
经 alembic upgrade 建表,本模块的 `Base.metadata` 供 env.py 的 autogenerate 收集。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Namespace(Base):
    __tablename__ = "namespace"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # =agentId
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 别名
    pull_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Service(Base):
    __tablename__ = "service"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace_id: Mapped[int] = mapped_column(Integer, index=True)
    service_code: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 别名
    dir: Mapped[str | None] = mapped_column(String(2048), nullable=True)  # compose 目录(命令下发)
    default_image: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    nacos_service_name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 滚动用(新增)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("namespace_id", "service_code", name="uq_service_ns_code"),)


class Plugin(Base):
    __tablename__ = "plugin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # npm 包名
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 别名
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PluginVersion(Base):
    __tablename__ = "plugin_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[str] = mapped_column(String(255))  # NOT NULL;= package.json.version
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("plugin_id", "version", name="uq_pv_plugin_version"),)


class PluginAttachment(Base):
    __tablename__ = "plugin_attachment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 评审 B1(方案 B):每 plugin_version 至多一附件(契合 spec version↔.tgz 一一对应)。
    # UNIQUE 后下载授权(plugin_version 粒度)与清单 func.max(attachment.id) 投放口径自然一致
    # (唯一行 → max 退化为该行)。UNIQUE 约束自带唯一索引,不再叠加普通 index。
    plugin_version_id: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(512))
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_path: Mapped[str] = mapped_column(String(1024))  # 平台生成
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("plugin_version_id", name="uq_pa_plugin_version"),)


class ServicePlugin(Base):
    __tablename__ = "service_plugin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("service_id", "plugin_id", name="uq_sp_service_plugin"),)


class ServicePluginVersion(Base):
    __tablename__ = "service_plugin_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_version_id: Mapped[int] = mapped_column(Integer, index=True)
    version_order: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_rolled_back: Mapped[bool] = mapped_column(Boolean, default=False)
    # 单活:app 维护的 nullable unique 普通列(active 时 = f"{service_id}-{plugin_id}",否则 NULL)。
    # 评审 C3:形态 `{service_id}-{plugin_id}`(int-int,最长 ~21 字符),收窄到 String(191)——
    # utf8mb4 下 UNIQUE 索引键长 ≤ 191*4=764 字节,远离 InnoDB 3072 字节上限(512 无必要且逼近上限)。
    spv_active_key: Mapped[str | None] = mapped_column(String(191), nullable=True, unique=True)
    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FetchRecord(Base):
    __tablename__ = "fetch_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace_id: Mapped[int] = mapped_column(Integer, index=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_id: Mapped[int] = mapped_column(Integer, index=True)
    plugin_version_id: Mapped[int] = mapped_column(Integer, index=True)
    fetch_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ServiceImage(Base):
    # 镜像台账(P4-4):一行 = 某 service 曾用/在用的一个镜像。同 service 多行(历史),
    # **至多一行 is_current=True**(当前镜像)。本期纯台账,不接 redeploy 寻址(后续 P4-2/P4-5)。
    # 单活由 app 在 set_current_image 内事务维护(同 service 先全清 is_current 再置目标),不设 DB
    # 生成列/唯一兜底——镜像历史并发写入低频,且 redeploy 寻址尚未消费此列,保持迁移简单(sqlite+MySQL8 双可建)。
    __tablename__ = "service_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)
    image: Mapped[str] = mapped_column(String(2048))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Rollout(Base):
    # 投放运行记录(P4-2/P4-3):一次「显式投放(Rollout)」= 把 desired-state 推到运行实例的一轮编排。
    #
    # 设计取舍(刻意「显式投放」而非「改 active 即自动滚」):发布/切版/切镜像只改 DB(维持现状),
    # 漂移在实例页/对账可见,由人显式发起投放或回滚 —— 故本表记录的是「投放动作」的运行态,
    # 与 service_plugin_version/service_images 的 desired-state 解耦(本期不动那些写端点)。
    #
    # frozen 语义(失败即停 / freeze):跨机滚动失败即停(failed→剩余 skipped,不回滚),留下「半迁移态」
    # (一部分实例已是新态、一部分仍旧态);此时把本行 frozen=True 标记为「冻结待人工」——提醒运维
    # 这次投放没全部完成、需人工 retry/rollback 收敛,而非系统自动续滚或回退(自动行为在半态下更危险)。
    #
    # 单活/锁不在本表:并发互斥复用 rolling_tasks.active_key(= serviceName)的 UNIQUE,本表不另立锁。
    __tablename__ = "rollouts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # uuid4 字符串
    namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    service_name: Mapped[str] = mapped_column(String(255), index=True)  # = nacosService
    # mode:'restart'(本期唯一可跑路径,走 graceful-restart 协调器)| 'pull-redeploy'(列保留两值,
    # 协调器尚不支持,路由层对其 422;待后续批次接入)。
    mode: Mapped[str] = mapped_column(String(32))
    # trigger:'manual' | 'publish' | 'rollback' | 'retry' —— 这次投放因何而起(审计 + 区分重试/回滚链)。
    trigger: Mapped[str] = mapped_column(String(32))
    target: Mapped[str | None] = mapped_column(Text, nullable=True)  # 本次投放 desired-state 的人读摘要
    previous_target: Mapped[str | None] = mapped_column(Text, nullable=True)  # 回滚参考(上一版人读摘要)
    # status:'running' | 'done' | 'degraded' | 'failed' —— 与底层 rolling_task 终态对齐回写。
    status: Mapped[str] = mapped_column(String(32))
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)  # 失败即冻结(半迁移态等人工)
    rolling_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 关联 rolling_tasks 运行
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    force: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
