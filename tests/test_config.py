"""Unit tests for configuration loading and validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from oracle_login_failure_monitor.config import AppConfig
from oracle_login_failure_monitor.main import OracleLoginFailureMonitor


def test_load_config_valid(tmp_path: Path) -> None:
    """Test loading a valid configuration file.

    Args:
        tmp_path (Path): Pytest temporary path fixture.
    """
    config_content = """
databases:
  - name: test-db
    dsn: localhost:1521/XE
    user: test_user
    password: test_password
max_workers: 4
refresh_seconds: 10
highlight_ttl: 2
log_file: "test_monitor.log"
log_level: "DEBUG"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content, encoding="utf-8")

    app_config = OracleLoginFailureMonitor.load_config(str(config_file))
    assert isinstance(app_config, AppConfig)
    assert len(app_config.databases) == 1
    assert app_config.databases[0].name == "test-db"
    assert app_config.databases[0].dsn == "localhost:1521/XE"
    assert app_config.databases[0].user == "test_user"
    assert app_config.databases[0].password == "test_password"
    assert app_config.max_workers == 4
    assert app_config.refresh_seconds == 10
    assert app_config.highlight_ttl == 2
    assert app_config.log_file == "test_monitor.log"
    assert app_config.log_level == "DEBUG"
    assert app_config.tcp_connect_timeout == 10
    assert app_config.query_timeout == 10


def test_load_config_file_not_found() -> None:
    """Test that FileNotFoundError is raised when config file does not exist."""
    with pytest.raises(FileNotFoundError):
        OracleLoginFailureMonitor.load_config("non_existent_file.yaml")


def test_load_config_empty(tmp_path: Path) -> None:
    """Test that ValueError is raised for an empty config file.

    Args:
        tmp_path (Path): Pytest temporary path fixture.
    """
    config_file = tmp_path / "empty.yaml"
    config_file.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="Config file is empty"):
        OracleLoginFailureMonitor.load_config(str(config_file))


def test_load_config_invalid_schema(tmp_path: Path) -> None:
    """Test that ValidationError is raised when schema is invalid.

    Args:
        tmp_path (Path): Pytest temporary path fixture.
    """
    config_content = """
databases:
  - name: test-db
    dsn: localhost:1521/XE
    user: test_user
    # missing password
max_workers: -1 # invalid, must be >= 1
"""
    config_file = tmp_path / "invalid.yaml"
    config_file.write_text(config_content, encoding="utf-8")

    with pytest.raises(ValidationError):
        OracleLoginFailureMonitor.load_config(str(config_file))


def test_load_config_extra_fields_forbidden(tmp_path: Path) -> None:
    """Test that extra fields not defined in AppConfig are forbidden.

    Args:
        tmp_path (Path): Pytest temporary path fixture.
    """
    config_content = """
databases:
  - name: test-db
    dsn: localhost:1521/XE
    user: test_user
    password: password
unknown_field: "not allowed"
"""
    config_file = tmp_path / "extra.yaml"
    config_file.write_text(config_content, encoding="utf-8")

    with pytest.raises(ValidationError):
        OracleLoginFailureMonitor.load_config(str(config_file))


def test_load_config_override_timeouts(tmp_path: Path) -> None:
    """Test that timeouts can be explicitly overridden in configuration.

    Args:
        tmp_path (Path): Pytest temporary path fixture.
    """
    config_content = """
databases:
  - name: test-db
    dsn: localhost:1521/XE
    user: test_user
    password: test_password
tcp_connect_timeout: 5
query_timeout: 8
"""
    config_file = tmp_path / "override.yaml"
    config_file.write_text(config_content, encoding="utf-8")

    app_config = OracleLoginFailureMonitor.load_config(str(config_file))
    assert app_config.tcp_connect_timeout == 5
    assert app_config.query_timeout == 8
