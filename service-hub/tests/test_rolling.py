import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database

def _db(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "t.db"))
    db.init_schema()
    return db

def test_rolling_tasks_table_created(tmp_path):
    db = _db(tmp_path)
    from sqlalchemy import inspect
    names = inspect(db.engine).get_table_names()
    assert "rolling_tasks" in names
    db.engine.dispose()
