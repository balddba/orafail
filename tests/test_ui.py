"""Unit tests for the dashboard UI rendering and layout components."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from rich.layout import Layout
from rich.table import Table

from oracle_login_failure_monitor.config import DatabaseConfig
from oracle_login_failure_monitor.main import OracleLoginFailureMonitor


@pytest.fixture
def mock_app_config() -> MagicMock:
    """Fixture to provide a mock AppConfig.

    Returns:
        MagicMock: A mocked configuration object.
    """
    mock_db = MagicMock(spec=DatabaseConfig)
    mock_db.name = "DB1"

    config = MagicMock()
    config.databases = [mock_db]
    config.max_workers = 3
    config.refresh_seconds = 10
    config.highlight_ttl = 2
    config.log_file = None
    config.log_level = "INFO"
    config.tcp_connect_timeout = 10
    config.query_timeout = 10
    return config


def test_trend_arrow() -> None:
    """Test that trend arrows are correctly returned based on count changes."""
    # Arrow for increasing counts
    assert OracleLoginFailureMonitor._trend_arrow(10, 5) == "[#ff5555]↑[/#ff5555]"
    # Arrow for decreasing counts
    assert OracleLoginFailureMonitor._trend_arrow(5, 10) == "[#50fa7b]↓[/#50fa7b]"
    # Arrow for unchanged counts
    assert OracleLoginFailureMonitor._trend_arrow(5, 5) == "[#ffb86c]→[/#ffb86c]"
    # Edge case: invalid types should return empty string
    assert OracleLoginFailureMonitor._trend_arrow("ERR", 5) == ""
    assert OracleLoginFailureMonitor._trend_arrow(5, None) == ""


def test_mark_new_rows(mock_app_config: MagicMock) -> None:
    """Test that new failure rows receive appropriate highlight styles.

    Args:
        mock_app_config (MagicMock): Mocked configuration fixture.
    """
    OracleLoginFailureMonitor.load_config = MagicMock(return_value=mock_app_config)
    monitor = OracleLoginFailureMonitor()

    db_name = "test-db"
    last_failed = datetime(2026, 7, 8, 12, 0, 0)
    row = {
        "user": "scott",
        "ip": "192.168.1.50",
        "last_failed_at": last_failed,
        "1m": 1,
        "10m": 1,
        "1h": 1,
    }

    # First cycle: Row is brand new, style should be bold red
    marked = monitor._mark_new_rows(db_name, [row])
    assert len(marked) == 1
    assert marked[0][0] == row
    assert marked[0][1] == "bold red"

    # Second cycle (age = 0 -> 1): Style should be bold yellow
    marked = monitor._mark_new_rows(db_name, [row])
    assert marked[0][1] == "bold yellow"

    # Third cycle (age = 1 -> 2): Style should be bold yellow
    marked = monitor._mark_new_rows(db_name, [row])
    assert marked[0][1] == "bold yellow"

    # Fourth cycle (age = 2 >= highlight_ttl): Style should be empty (expired)
    marked = monitor._mark_new_rows(db_name, [row])
    assert marked[0][1] == ""


def test_build_summary_table(mock_app_config: MagicMock) -> None:
    """Test building the summary table layout.

    Args:
        mock_app_config (MagicMock): Mocked configuration fixture.
    """
    OracleLoginFailureMonitor.load_config = MagicMock(return_value=mock_app_config)
    monitor = OracleLoginFailureMonitor()

    results = {
        "DB1": {
            "status": "ONLINE",
            "latency_ms": 15,
            "1m": 0,
            "10m": 5,
            "1h": 12,
            "details": [],
        },
        "DB2": {
            "status": "OFFLINE",
            "latency_ms": None,
            "1m": "ERR",
            "10m": "ERR",
            "1h": "ERR",
            "details": [],
        },
    }
    previous = {
        "DB1": {
            "1m": 0,
            "10m": 2,
            "1h": 10,
        }
    }

    table = monitor._build_summary_table(results, previous)
    assert isinstance(table, Table)
    assert len(table.rows) == 2
    assert len(table.columns) == 6


def test_build_layout(mock_app_config: MagicMock) -> None:
    """Test that the full layout is successfully generated with sub-layouts.

    Args:
        mock_app_config (MagicMock): Mocked configuration fixture.
    """
    OracleLoginFailureMonitor.load_config = MagicMock(return_value=mock_app_config)
    monitor = OracleLoginFailureMonitor()

    results = {
        "DB1": {
            "status": "ONLINE",
            "latency_ms": 25,
            "1m": 2,
            "10m": 4,
            "1h": 8,
            "details": [
                {
                    "user": "sys",
                    "ip": "10.0.0.5",
                    "last_failed_at": datetime.now(),
                    "1m": 1,
                    "10m": 2,
                    "1h": 3,
                }
            ],
        }
    }

    layout = monitor._build_layout(results, {})
    assert isinstance(layout, Layout)
    assert layout["header"] is not None
    assert layout["body"] is not None
    assert layout["logs"] is not None
    assert layout["footer"] is not None


def test_connection_caching_and_reuse(mock_app_config: MagicMock) -> None:
    """Test that connections are cached and reused, with pinging for safety."""
    from unittest.mock import patch

    OracleLoginFailureMonitor.load_config = MagicMock(return_value=mock_app_config)
    monitor = OracleLoginFailureMonitor()

    db_config = mock_app_config.databases[0]
    db_config.user = "test_user"
    db_config.password = "test_pass"
    db_config.dsn = "test_dsn"

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (0, 0, 0)
    mock_cursor.fetchall.return_value = []

    with patch("oracledb.connect", return_value=mock_conn) as mock_connect:
        # First call: connection is created and cached
        res = monitor._fetch_failed_logins(db_config)
        assert res["status"] == "ONLINE"
        assert mock_connect.call_count == 1
        assert db_config.name in monitor._connections
        assert monitor._connections[db_config.name] == mock_conn

        # Second call: connection is reused and pinged, no new connect
        res = monitor._fetch_failed_logins(db_config)
        assert res["status"] == "ONLINE"
        assert mock_connect.call_count == 1
        mock_conn.ping.assert_called_once()

        # Third call: ping fails, connection is closed, and a new one is created
        mock_conn.ping.side_effect = Exception("Connection dead")
        mock_conn_new = MagicMock()
        mock_conn_new.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn_new

        res = monitor._fetch_failed_logins(db_config)
        assert res["status"] == "ONLINE"
        assert mock_connect.call_count == 2
        assert monitor._connections[db_config.name] == mock_conn_new

        # Shutdown monitor closes connections
        monitor.close()
        mock_conn_new.close.assert_called_once()
        assert len(monitor._connections) == 0


def test_fetch_all_query_timeout(mock_app_config: MagicMock) -> None:
    """Test that query timeouts are handled gracefully in _fetch_all."""
    import time
    from unittest.mock import patch

    mock_app_config.query_timeout = 1
    OracleLoginFailureMonitor.load_config = MagicMock(return_value=mock_app_config)
    monitor = OracleLoginFailureMonitor()

    def slow_fetch(db):
        time.sleep(2)
        return {"status": "ONLINE"}

    with patch.object(monitor, "_fetch_failed_logins", side_effect=slow_fetch):
        results = monitor._fetch_all()
        assert monitor.databases[0].name in results
        assert results[monitor.databases[0].name]["status"] == "OFFLINE"
        assert results[monitor.databases[0].name]["1m"] == "TIMEOUT"


def test_headless_run_once(mock_app_config: MagicMock) -> None:
    """Test that run() behaves correctly in headless mode and breaks on loop."""
    from unittest.mock import patch

    OracleLoginFailureMonitor.load_config = MagicMock(return_value=mock_app_config)
    monitor = OracleLoginFailureMonitor(headless=True)

    dummy_result = {
        "DB1": {
            "status": "ONLINE",
            "latency_ms": 10,
            "1m": 0,
            "10m": 0,
            "1h": 0,
            "details": [],
        }
    }

    # Use side_effect to raise KeyboardInterrupt on the second loop iteration/sleep
    with (
        patch.object(monitor, "_fetch_all", return_value=dummy_result) as mock_fetch,
        patch("time.sleep", side_effect=KeyboardInterrupt) as mock_sleep,
    ):
        try:
            monitor.run()
        except KeyboardInterrupt:
            pass

        mock_fetch.assert_called_once()
        mock_sleep.assert_called_once()
