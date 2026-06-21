from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import Database
from app.db_models import CommandModel
from app.store import utc_now


def test_command_model_persists_and_reads_back_mode(tmp_path: Path) -> None:
    # mode 列须随迁移落库并能 round-trip(graceful/force/None)
    database = Database(f"sqlite:///{tmp_path / 'mode.db'}")
    database.init_schema()

    now = utc_now()
    with database.session_factory() as session:
        session.add_all(
            [
                CommandModel(
                    request_id="cmd-graceful",
                    agent_id="agent-a",
                    action="stop",
                    target_dir="/srv/a",
                    status="queued",
                    payload_json="{}",
                    mode="graceful",
                    created_at=now,
                    updated_at=now,
                ),
                CommandModel(
                    request_id="cmd-force",
                    agent_id="agent-a",
                    action="pull-redeploy",
                    target_dir="/srv/a",
                    target_image="nginx:latest",
                    status="queued",
                    payload_json="{}",
                    mode="force",
                    created_at=now,
                    updated_at=now,
                ),
                CommandModel(
                    request_id="cmd-no-mode",
                    agent_id="agent-a",
                    action="restart",
                    target_dir="/srv/a",
                    status="queued",
                    payload_json="{}",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.commit()

    with database.session_factory() as session:
        graceful = session.scalar(select(CommandModel).where(CommandModel.request_id == "cmd-graceful"))
        force = session.scalar(select(CommandModel).where(CommandModel.request_id == "cmd-force"))
        no_mode = session.scalar(select(CommandModel).where(CommandModel.request_id == "cmd-no-mode"))

    assert graceful is not None and graceful.mode == "graceful"
    assert force is not None and force.mode == "force"
    assert no_mode is not None and no_mode.mode is None


def test_init_schema_stamps_fully_initialized_legacy_schema(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'legacy.db'}")
    stamp_calls: list[tuple[object, str]] = []
    upgrade_calls: list[tuple[object, str]] = []

    class Inspector:
        def get_table_names(self) -> list[str]:
            return ["agents", "commands", "command_events", "rolling_tasks"]

    monkeypatch.setattr("app.db.inspect", lambda engine: Inspector())
    monkeypatch.setattr("app.db.command.stamp", lambda config, revision: stamp_calls.append((config, revision)))
    monkeypatch.setattr("app.db.command.upgrade", lambda config, revision: upgrade_calls.append((config, revision)))

    database.init_schema()

    assert len(stamp_calls) == 1
    assert stamp_calls[0][1] == "head"
    assert upgrade_calls == []


def test_init_schema_rejects_partially_initialized_legacy_schema(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'partial.db'}")

    class Inspector:
        def get_table_names(self) -> list[str]:
            return ["agents"]

    monkeypatch.setattr("app.db.inspect", lambda engine: Inspector())

    with pytest.raises(RuntimeError, match="partially initialized legacy schema"):
        database.init_schema()


def test_init_schema_runs_upgrade_for_new_schema(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    upgrade_calls: list[tuple[object, str]] = []

    class Inspector:
        def get_table_names(self) -> list[str]:
            return []

    monkeypatch.setattr("app.db.inspect", lambda engine: Inspector())
    monkeypatch.setattr("app.db.command.upgrade", lambda config, revision: upgrade_calls.append((config, revision)))

    database.init_schema()

    assert len(upgrade_calls) == 1
    assert upgrade_calls[0][1] == "head"


def test_database_helpers_handle_sqlite_paths_and_build_config(tmp_path: Path) -> None:
    database_file = tmp_path / "nested" / "hub.db"
    database = Database(f"sqlite:///{database_file}")

    assert database_file.parent.exists()

    config = database._build_alembic_config()

    assert config.get_main_option("sqlalchemy.url") == f"sqlite:///{database_file}"
    assert config.get_main_option("script_location").endswith("migrations")

    memory_database = Database("sqlite:///:memory:")
    memory_database._ensure_sqlite_parent_dir("sqlite:///:memory:")

    postgres_database = Database(f"sqlite:///{tmp_path / 'other.db'}")
    postgres_database._ensure_sqlite_parent_dir("postgresql://user:pass@localhost/test")
