import importlib
import sys

import pytest


def test_config_import_tolerates_missing_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    sys.modules.pop("app.config", None)

    module = importlib.import_module("app.config")

    assert module.settings.admin_token == ""


def test_rolling_defaults(monkeypatch):
    for k in ("ROLLING_SETTLE_SEC", "ROLLING_SHUTDOWN_TIMEOUT", "ROLLING_READY_TIMEOUT", "ROLLING_CMD_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    import importlib
    import app.config as config
    importlib.reload(config)
    s = config.Settings()
    assert s.rolling_settle_sec == 35
    assert s.rolling_shutdown_timeout == 60
    assert s.rolling_ready_timeout == 180
    assert s.rolling_cmd_timeout == 480