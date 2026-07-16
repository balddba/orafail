"""Unit tests for the dashboard UI rendering and layout components."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr
from rich.layout import Layout
from rich.table import Table

from orafail.config import DatabaseConfig
from orafail.main import OracleLoginFailureMonitor
from orafail.models.all_results import AllResults
from orafail.models.database_result import DatabaseResult
from orafail.models.failure_detail import FailureDetail


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
    config.sort_by = "time"
    return config


def test_trend_arrow() -> None:
    """Test that trend arrows are correctly returned based on count changes."""
    # Arrow for increasing counts
    assert OracleLoginFailureMonitor._trend_arrow(10, 5) == "[#f38ba8]↑[/#f38ba8]"
    # Arrow for decreasing counts
    assert OracleLoginFailureMonitor._trend_arrow(5, 10) == "[#a6e3a1]↓[/#a6e3a1]"
    # Arrow for unchanged counts
    assert OracleLoginFailureMonitor._trend_arrow(5, 5) == "[#f9e2af]→[/#f9e2af]"
    # Edge case: invalid types should return empty string
    assert OracleLoginFailureMonitor._trend_arrow("ERR", 5) == ""
    assert OracleLoginFailureMonitor._trend_arrow(5, None) == ""


def test_mark_new_rows(
    mock_app_config: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that new failure rows receive appropriate highlight styles.

    Args:
        mock_app_config (MagicMock): Mocked configuration fixture.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        OracleLoginFailureMonitor,
        "load_config",
        MagicMock(return_value=mock_app_config),
    )
    monitor = OracleLoginFailureMonitor()

    db_name = "test-db"
    last_failed = datetime(2026, 7, 8, 12, 0, 0)
    row = FailureDetail(
        user="scott",
        ip="192.168.1.50",
        last_failed_at=last_failed,
        m1=1,
        m10=1,
        h1=1,
    )

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


def test_build_summary_table(
    mock_app_config: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test building the summary table layout.

    Args:
        mock_app_config (MagicMock): Mocked configuration fixture.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        OracleLoginFailureMonitor,
        "load_config",
        MagicMock(return_value=mock_app_config),
    )
    monitor = OracleLoginFailureMonitor()

    results = AllResults(
        results={
            "DB1": DatabaseResult(
                status="ONLINE",
                latency_ms=15,
                m1=0,
                m10=5,
                h1=12,
                details=[],
            ),
            "DB2": DatabaseResult(
                status="OFFLINE",
                latency_ms=None,
                m1="ERR",
                m10="ERR",
                h1="ERR",
                details=[],
            ),
        }
    )
    previous = AllResults(
        results={
            "DB1": DatabaseResult(
                status="ONLINE",
                latency_ms=10,
                m1=0,
                m10=2,
                h1=10,
                details=[],
            )
        }
    )

    table = monitor._build_summary_table(results, previous)
    assert isinstance(table, Table)
    assert len(table.rows) == 2
    assert len(table.columns) == 6


def test_build_detail_tables(
    mock_app_config: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test building the single detailed failures table.

    Args:
        mock_app_config (MagicMock): Mocked configuration fixture.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        OracleLoginFailureMonitor,
        "load_config",
        MagicMock(return_value=mock_app_config),
    )
    monitor = OracleLoginFailureMonitor()

    results = AllResults(
        results={
            "DB1": DatabaseResult(
                status="ONLINE",
                latency_ms=25,
                m1=2,
                m10=4,
                h1=8,
                details=[
                    FailureDetail(
                        user="sys",
                        ip="10.0.0.5",
                        last_failed_at=datetime(2026, 7, 16, 10, 0, 0),
                        m1=1,
                        m10=2,
                        h1=3,
                    )
                ],
            ),
            "DB2": DatabaseResult(
                status="ONLINE",
                latency_ms=30,
                m1=1,
                m10=2,
                h1=3,
                details=[
                    FailureDetail(
                        user="system",
                        ip="10.0.0.6",
                        last_failed_at=datetime(2026, 7, 16, 11, 0, 0),
                        m1=1,
                        m10=1,
                        h1=1,
                    )
                ],
            ),
        }
    )

    table = monitor._build_detail_tables(results)
    assert isinstance(table, Table)
    assert len(table.rows) == 2
    assert len(table.columns) == 7

    # Since it is sorted by last_failed_at descending:
    # DB2's event (11:00:00) should be first, and DB1's event (10:00:00) should be second.
    db_cells = list(table.columns[0].cells)
    user_cells = list(table.columns[1].cells)
    last_failed_cells = list(table.columns[3].cells)

    assert db_cells == [
        "[bold #89b4fa]DB2[/bold #89b4fa]",
        "[bold #cba6f7]DB1[/bold #cba6f7]",
    ]
    assert user_cells == [
        "[bold #b4befe]system[/bold #b4befe]",
        "[bold #f2cdcd]sys[/bold #f2cdcd]",
    ]
    # Both are brand new failures, so style should be bold pink (#f5c2e7)
    assert last_failed_cells == [
        "[bold #f5c2e7]2026-07-16 11:00:00[/bold #f5c2e7]",
        "[bold #f5c2e7]2026-07-16 10:00:00[/bold #f5c2e7]",
    ]

    # Second call (age 0 -> 1): style should be dim pink (dim #f5c2e7)
    table_aging = monitor._build_detail_tables(results)
    last_failed_cells_aging = list(table_aging.columns[3].cells)
    assert last_failed_cells_aging == [
        "[dim #f5c2e7]2026-07-16 11:00:00[/dim #f5c2e7]",
        "[dim #f5c2e7]2026-07-16 10:00:00[/dim #f5c2e7]",
    ]

    # Third call (age 1 -> 2): style should be dim pink (dim #f5c2e7)
    table_aging2 = monitor._build_detail_tables(results)
    last_failed_cells_aging2 = list(table_aging2.columns[3].cells)
    assert last_failed_cells_aging2 == [
        "[dim #f5c2e7]2026-07-16 11:00:00[/dim #f5c2e7]",
        "[dim #f5c2e7]2026-07-16 10:00:00[/dim #f5c2e7]",
    ]

    # Fourth call (age 2 >= highlight_ttl): style should be expired (dim #a6adc8)
    table_expired = monitor._build_detail_tables(results)
    last_failed_cells_expired = list(table_expired.columns[3].cells)
    assert last_failed_cells_expired == [
        "[dim #a6adc8]2026-07-16 11:00:00[/dim #a6adc8]",
        "[dim #a6adc8]2026-07-16 10:00:00[/dim #a6adc8]",
    ]

    # Test with no results/empty dictionary
    table_empty = monitor._build_detail_tables(AllResults(results={}))
    assert isinstance(table_empty, Table)
    assert len(table_empty.rows) == 1
    assert (
        list(table_empty.columns[0].cells)[0]
        == "[dim]Poller starting or no database configured...[/dim]"
    )


def test_build_detail_tables_sorting(
    mock_app_config: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test building the single detailed failures table with different sort modes.

    Args:
        mock_app_config (MagicMock): Mocked configuration fixture.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        OracleLoginFailureMonitor,
        "load_config",
        MagicMock(return_value=mock_app_config),
    )

    results = AllResults(
        results={
            "DB_A": DatabaseResult(
                status="ONLINE",
                latency_ms=25,
                m1=2,
                m10=4,
                h1=8,
                details=[
                    FailureDetail(
                        user="user_b",
                        ip="10.0.0.5",
                        last_failed_at=datetime(2026, 7, 16, 10, 0, 0),
                        m1=5,
                        m10=5,
                        h1=5,
                    )
                ],
            ),
            "DB_B": DatabaseResult(
                status="ONLINE",
                latency_ms=30,
                m1=1,
                m10=2,
                h1=3,
                details=[
                    FailureDetail(
                        user="user_a",
                        ip="10.0.0.6",
                        last_failed_at=datetime(2026, 7, 16, 11, 0, 0),
                        m1=10,
                        m10=10,
                        h1=10,
                    )
                ],
            ),
        }
    )

    # 1. Sort by time (default: descending)
    monitor_time = OracleLoginFailureMonitor(sort_by="time")
    table_time = monitor_time._build_detail_tables(results)
    db_cells_time = list(table_time.columns[0].cells)
    # DB_B is newer (11:00) so it should be first
    assert "DB_B" in db_cells_time[0]
    assert "DB_A" in db_cells_time[1]

    # 2. Sort by user (ascending: user_a, user_b)
    monitor_user = OracleLoginFailureMonitor(sort_by="user")
    table_user = monitor_user._build_detail_tables(results)
    user_cells_user = list(table_user.columns[1].cells)
    # user_a is first, user_b is second
    assert "user_a" in user_cells_user[0]
    assert "user_b" in user_cells_user[1]

    # 3. Sort by database (ascending: DB_A, DB_B)
    monitor_db = OracleLoginFailureMonitor(sort_by="database")
    table_db = monitor_db._build_detail_tables(results)
    db_cells_db = list(table_db.columns[0].cells)
    # DB_A is first, DB_B is second
    assert "DB_A" in db_cells_db[0]
    assert "DB_B" in db_cells_db[1]

    # 4. Sort by count (descending 1m count: user_a has 10, user_b has 5)
    monitor_count = OracleLoginFailureMonitor(sort_by="count")
    table_count = monitor_count._build_detail_tables(results)
    user_cells_count = list(table_count.columns[1].cells)
    # user_a has 10 count, user_b has 5 count. user_a should be first.
    assert "user_a" in user_cells_count[0]
    assert "user_b" in user_cells_count[1]


def test_build_layout(
    mock_app_config: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that the full layout is successfully generated with sub-layouts.

    Args:
        mock_app_config (MagicMock): Mocked configuration fixture.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        OracleLoginFailureMonitor,
        "load_config",
        MagicMock(return_value=mock_app_config),
    )
    monitor = OracleLoginFailureMonitor()

    results = AllResults(
        results={
            "DB1": DatabaseResult(
                status="ONLINE",
                latency_ms=25,
                m1=2,
                m10=4,
                h1=8,
                details=[
                    FailureDetail(
                        user="sys",
                        ip="10.0.0.5",
                        last_failed_at=datetime.now(),
                        m1=1,
                        m10=2,
                        h1=3,
                    )
                ],
            )
        }
    )

    layout = monitor._build_layout(results, AllResults(results={}))
    assert isinstance(layout, Layout)
    assert layout["header"] is not None
    assert layout["body"] is not None
    assert layout["logs"] is not None
    assert layout["footer"] is not None


def test_connection_caching_and_reuse(
    mock_app_config: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that connections are cached and reused, with pinging for safety.

    Args:
        mock_app_config (MagicMock): Mocked configuration fixture.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    from unittest.mock import patch

    monkeypatch.setattr(
        OracleLoginFailureMonitor,
        "load_config",
        MagicMock(return_value=mock_app_config),
    )
    monitor = OracleLoginFailureMonitor()

    db_config = mock_app_config.databases[0]
    db_config.user = "test_user"
    db_config.password = SecretStr("test_pass")
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


def test_fetch_all_query_timeout(
    mock_app_config: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that query timeouts are handled gracefully in _fetch_all.

    Args:
        mock_app_config (MagicMock): Mocked configuration fixture.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    import time
    from unittest.mock import patch

    mock_app_config.query_timeout = 1
    monkeypatch.setattr(
        OracleLoginFailureMonitor,
        "load_config",
        MagicMock(return_value=mock_app_config),
    )
    monitor = OracleLoginFailureMonitor()

    def slow_fetch(db):
        time.sleep(2)
        return DatabaseResult(
            status="ONLINE",
            latency_ms=10,
            m1=0,
            m10=0,
            h1=0,
            details=[],
        )

    with patch.object(monitor, "_fetch_failed_logins", side_effect=slow_fetch):
        results = monitor._fetch_all()
        assert monitor.databases[0].name in results
        assert results[monitor.databases[0].name]["status"] == "OFFLINE"
        assert results[monitor.databases[0].name]["1m"] == "TIMEOUT"


def test_headless_run_once(
    mock_app_config: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that run() behaves correctly in headless mode and breaks on loop.

    Args:
        mock_app_config (MagicMock): Mocked configuration fixture.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    from unittest.mock import patch

    monkeypatch.setattr(
        OracleLoginFailureMonitor,
        "load_config",
        MagicMock(return_value=mock_app_config),
    )
    monitor = OracleLoginFailureMonitor(headless=True)

    dummy_result = AllResults(
        results={
            "DB1": DatabaseResult(
                status="ONLINE",
                latency_ms=10,
                m1=0,
                m10=0,
                h1=0,
                details=[],
            )
        }
    )

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
