"""Unit tests for the Demo mode functionality in OraFail."""

from pathlib import Path
from unittest.mock import patch

from orafail.config import AppConfig
from orafail.main import DEMO_DATABASES, OracleLoginFailureMonitor
from orafail.models.database_result import DatabaseResult


def test_load_config_demo_missing_file() -> None:
    """Test load_config in demo mode when the configuration file is missing."""
    config = OracleLoginFailureMonitor.load_config(
        "non_existent_config.yaml", demo=True
    )
    assert isinstance(config, AppConfig)
    assert len(config.databases) == len(DEMO_DATABASES)
    assert config.databases[0].name == "prod-oltp"
    assert config.databases[1].name == "dw-warehouse"
    assert config.databases[2].name == "auth-db"


def test_load_config_demo_empty_file(tmp_path: Path) -> None:
    """Test load_config in demo mode when the configuration file is empty.

    Args:
        tmp_path (Path): Pytest temporary path fixture.
    """
    config_file = tmp_path / "empty.yaml"
    config_file.write_text("", encoding="utf-8")

    config = OracleLoginFailureMonitor.load_config(str(config_file), demo=True)
    assert isinstance(config, AppConfig)
    assert len(config.databases) == len(DEMO_DATABASES)


def test_load_config_demo_override_databases(tmp_path: Path) -> None:
    """Test load_config in demo mode when the config file exists and contains other settings.

    Args:
        tmp_path (Path): Pytest temporary path fixture.
    """
    config_content = """
databases:
  - name: real-db
    dsn: localhost:1521/XE
    user: real_user
    password: real_password
max_workers: 9
refresh_seconds: 42
highlight_ttl: 5
"""
    config_file = tmp_path / "demo_override.yaml"
    config_file.write_text(config_content, encoding="utf-8")

    config = OracleLoginFailureMonitor.load_config(str(config_file), demo=True)
    assert isinstance(config, AppConfig)
    # The real database configuration should be replaced by DEMO_DATABASES
    assert len(config.databases) == len(DEMO_DATABASES)
    assert config.databases[0].name == "prod-oltp"
    # But the other settings should remain intact
    assert config.max_workers == 9
    assert config.refresh_seconds == 42
    assert config.highlight_ttl == 5


def test_monitor_init_demo(tmp_path: Path) -> None:
    """Test that monitor initialization correctly sets up synthetic history.

    Args:
        tmp_path (Path): Pytest temporary path fixture.
    """
    config_file = tmp_path / "config.yaml"
    config_file.write_text("refresh_seconds: 15", encoding="utf-8")

    monitor = OracleLoginFailureMonitor(
        config_path=str(config_file), headless=True, demo=True
    )
    assert monitor.demo is True
    assert len(monitor._synthetic_failures) == len(DEMO_DATABASES)

    # Check that each database is pre-populated with events
    for db in DEMO_DATABASES:
        failures = monitor._synthetic_failures[db.name]
        assert len(failures) >= 5
        assert len(failures) <= 15
        for event in failures:
            assert "user" in event
            assert "ip" in event
            assert "last_failed_at" in event


def test_fetch_failed_logins_demo() -> None:
    """Test that _fetch_failed_logins generates valid results in demo mode."""
    # Initialize with default settings in demo mode
    monitor = OracleLoginFailureMonitor(
        config_path="non_existent_config.yaml", headless=True, demo=True
    )

    db = DEMO_DATABASES[0]
    # Fetch first time (ONLINE)
    with patch("random.random", return_value=0.5):  # No offline status, no new event
        res = monitor._fetch_failed_logins(db)
        assert isinstance(res, DatabaseResult)
        assert res.status == "ONLINE"
        assert isinstance(res.latency_ms, int)
        assert res.latency_ms >= 20
        assert res.latency_ms <= 120

    # Test new event generation
    initial_count = len(monitor._synthetic_failures[db.name])
    with patch("random.random", return_value=0.1):  # Triggers a new event
        res = monitor._fetch_failed_logins(db)
        assert len(monitor._synthetic_failures[db.name]) == initial_count + 1
        assert res.status == "ONLINE"


def test_fetch_failed_logins_demo_offline() -> None:
    """Test that dw-warehouse can go offline in demo mode."""
    monitor = OracleLoginFailureMonitor(
        config_path="non_existent_config.yaml", headless=True, demo=True
    )

    db_dw = next(db for db in DEMO_DATABASES if db.name == "dw-warehouse")
    with patch("random.random", return_value=0.01):  # Triggers offline status
        res = monitor._fetch_failed_logins(db_dw)
        assert res.status == "OFFLINE"
        assert res.latency_ms is None
        assert res.m1 == "ERR"
        assert res.m10 == "ERR"
        assert res.h1 == "ERR"
        assert len(res.details) == 0
