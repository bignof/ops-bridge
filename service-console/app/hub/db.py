from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    _managed_tables = {"agents", "commands", "command_events", "rolling_tasks"}

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._ensure_sqlite_parent_dir(database_url)

        url = make_url(database_url)
        connect_args = {"check_same_thread": False} if url.get_backend_name() == "sqlite" else {}

        self.engine = create_engine(
            database_url,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def init_schema(self) -> None:
        # S2 过渡:hub 表暂用独立 DB,直接按 db_models 的 Base.metadata 建表(hub 的 alembic migrations
        # 未随 app 一起并入 console)。**S4 合 DB 时改走合并后单一 alembic**,并恢复 _managed_tables
        # legacy 守卫 + 列集断言(评审 H-1)。本过渡库为独立 fresh 库,无 legacy 冲突。
        from app.hub import db_models  # noqa: F401 (注册所有表到 Base.metadata)

        Base.metadata.create_all(self.engine)

    def ping(self) -> bool:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True

    def _ensure_sqlite_parent_dir(self, database_url: str) -> None:
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite":
            return

        database = url.database
        if not database or database == ":memory:":
            return

        Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    def _build_alembic_config(self) -> Config:
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "migrations"))
        config.set_main_option("sqlalchemy.url", self.database_url)
        return config
