from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    _managed_tables = {"agents", "commands", "command_events"}

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
        config = self._build_alembic_config()
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())

        if "alembic_version" not in existing_tables and self._managed_tables <= existing_tables:
            command.stamp(config, "head")
            return

        if "alembic_version" not in existing_tables and self._managed_tables & existing_tables:
            raise RuntimeError("Detected partially initialized legacy schema; manual intervention is required before migration")

        command.upgrade(config, "head")

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
