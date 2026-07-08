"""Render a live dashboard of failed Oracle login events."""

import argparse
from collections import deque
import concurrent.futures
from datetime import datetime
from pathlib import Path
import threading
import time
from typing import Any, TypeAlias

import oracledb
import yaml
from loguru import logger
from rich.align import Align
from rich.box import ROUNDED, SIMPLE
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from orafail.config import AppConfig, DatabaseConfig

FailureDetail: TypeAlias = dict[str, Any]
DatabaseResult: TypeAlias = dict[str, int | str | list[FailureDetail]]
AllResults: TypeAlias = dict[str, DatabaseResult]
EventKey: TypeAlias = tuple[str, Any, Any, Any]


SUMMARY_QUERY = """
SELECT
  SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '1' MINUTE THEN 1 ELSE 0 END),
  SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '10' MINUTE THEN 1 ELSE 0 END),
  SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '1' HOUR THEN 1 ELSE 0 END)
FROM unified_audit_trail
WHERE action_name = 'LOGON'
AND return_code != 0
"""

DETAIL_QUERY = """
SELECT *
FROM (
  SELECT
    dbusername,
    userhost,
    MAX(event_timestamp) AS last_failed_at,
    SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '1' MINUTE THEN 1 ELSE 0 END) AS cnt_1m,
    SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '10' MINUTE THEN 1 ELSE 0 END) AS cnt_10m,
    SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '1' HOUR THEN 1 ELSE 0 END) AS cnt_1h
  FROM unified_audit_trail
  WHERE action_name = 'LOGON'
    AND return_code != 0
  GROUP BY dbusername, userhost
  ORDER BY last_failed_at DESC
)
WHERE ROWNUM <= 5
"""


class OracleLoginFailureMonitor:
    """Monitor and render Oracle login failures in a live terminal dashboard.

    Attributes:
        app_config (AppConfig): Runtime configuration for refresh and display behavior.
        databases (list[DatabaseConfig]): Target Oracle databases to query.
        highlight_ttl (int): Number of refresh cycles to keep row highlights visible.
        previous_results (AllResults): Most recent successful snapshot for trend arrows.
        seen_events (dict[str, set[EventKey]]): Per-database event keys already observed.
        event_age (dict[EventKey, int]): Highlight age counters for known events.
    """

    @staticmethod
    def load_config(path: str = "config.yaml") -> AppConfig:
        """Load and validate dashboard configuration from YAML.

        Args:
            path (str): Path to the YAML file.

        Returns:
            AppConfig: Parsed and validated application configuration.

        Raises:
            FileNotFoundError: If the YAML file path does not exist.
            ValueError: If the YAML file is empty or invalid for AppConfig.
        """
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with config_path.open("r", encoding="utf-8") as config_file:
            raw_config: dict[str, Any] | None = yaml.safe_load(config_file)

        if raw_config is None:
            raise ValueError(f"Config file is empty: {config_path}")

        return AppConfig.model_validate(raw_config)

    def __init__(
        self, config_path: str = "config.yaml", headless: bool = False
    ) -> None:
        """Initialize monitor state by loading configuration.

        Args:
            config_path (str): Path to the YAML configuration file.
            headless (bool): Run in headless daemon mode (no terminal UI).
        """
        self.app_config = self.load_config(config_path)
        self.databases = self.app_config.databases
        self.highlight_ttl = self.app_config.highlight_ttl
        self.previous_results: AllResults = {}
        self.seen_events: dict[str, set[EventKey]] = {}
        self.event_age: dict[EventKey, int] = {}
        self.log_messages: deque[str] = deque(maxlen=5)
        self.headless = headless
        self._connections: dict[str, oracledb.Connection] = {}
        self._conn_lock = threading.Lock()

        if not self.headless:
            # Remove default logger to prevent screen corruption in alternate screen mode
            logger.remove()

        if self.app_config.log_file:
            logger.add(
                self.app_config.log_file,
                level=self.app_config.log_level,
                rotation="10 MB",
                retention="5 days",
            )

        if not self.headless:
            # Capture loguru logs in the in-memory queue for display in the dashboard
            logger.add(
                lambda msg: self.log_messages.append(str(msg).strip()),
                level="INFO",
                format="{time:HH:mm:ss} | {level: <7} | {message}",
                backtrace=False,
                diagnose=False,
            )

    def _fetch_failed_logins(self, db: DatabaseConfig) -> DatabaseResult:
        """Query one database for failed login summaries and details.

        Args:
            db (DatabaseConfig): Connection details for the target database.

        Returns:
            DatabaseResult: Summary counters and detail rows for one database.
        """
        start_time = time.perf_counter()
        conn = None
        try:
            with self._conn_lock:
                conn = self._connections.get(db.name)
                if conn is not None:
                    try:
                        conn.ping()
                    except Exception:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None

                if conn is None:
                    conn = oracledb.connect(
                        user=db.user,
                        password=db.password,
                        dsn=db.dsn,
                        tcp_connect_timeout=self.app_config.tcp_connect_timeout,
                    )
                    self._connections[db.name] = conn

            cursor = conn.cursor()

            cursor.execute(SUMMARY_QUERY)
            summary_row = cursor.fetchone()

            cursor.execute(DETAIL_QUERY)
            detail_rows = cursor.fetchall()
            details = [
                {
                    "user": row[0],
                    "ip": row[1],
                    "last_failed_at": row[2],
                    "1m": row[3] or 0,
                    "10m": row[4] or 0,
                    "1h": row[5] or 0,
                }
                for row in detail_rows
            ]

            cursor.close()

            latency_ms = int((time.perf_counter() - start_time) * 1000)

            return {
                "status": "ONLINE",
                "latency_ms": latency_ms,
                "1m": summary_row[0] or 0,
                "10m": summary_row[1] or 0,
                "1h": summary_row[2] or 0,
                "details": details,
            }
        except Exception as e:
            logger.error(f"Failed to query database {db.name}: {e}")
            with self._conn_lock:
                if db.name in self._connections:
                    try:
                        self._connections[db.name].close()
                    except Exception:
                        pass
                    del self._connections[db.name]
            return {
                "status": "OFFLINE",
                "latency_ms": None,
                "1m": "ERR",
                "10m": "ERR",
                "1h": "ERR",
                "details": [],
            }

    def close(self) -> None:
        """Close all cached database connections."""
        with self._conn_lock:
            for db_name, conn in list(self._connections.items()):
                try:
                    conn.close()
                except Exception as e:
                    logger.debug(f"Error closing connection for {db_name}: {e}")
            self._connections.clear()

    def _fetch_all(self) -> AllResults:
        """Fetch failed-login data for all configured databases concurrently.

        Returns:
            AllResults: Per-database query results for the dashboard.
        """
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.app_config.max_workers
        ) as executor:
            futures = {
                executor.submit(self._fetch_failed_logins, db): db
                for db in self.databases
            }

            done, not_done = concurrent.futures.wait(
                futures.keys(),
                timeout=float(self.app_config.query_timeout),
            )

            results: AllResults = {}

            # Process successfully completed queries
            for future in done:
                db = futures[future]
                try:
                    results[db.name] = future.result()
                except Exception as e:
                    logger.error(f"Error fetching logs for {db.name}: {e}")
                    results[db.name] = {
                        "status": "OFFLINE",
                        "latency_ms": None,
                        "1m": "ERR",
                        "10m": "ERR",
                        "1h": "ERR",
                        "details": [],
                    }

            # Process timed out queries
            for future in not_done:
                db = futures[future]
                logger.warning(
                    f"Query timed out for database {db.name} after {self.app_config.query_timeout}s"
                )
                results[db.name] = {
                    "status": "OFFLINE",
                    "latency_ms": None,
                    "1m": "TIMEOUT",
                    "10m": "TIMEOUT",
                    "1h": "TIMEOUT",
                    "details": [],
                }

            return results

    @staticmethod
    def _trend_arrow(current: Any, previous: Any) -> str:
        """Get trend arrow for visual indicator.

        Args:
            current (Any): Current count.
            previous (Any): Previous count.

        Returns:
            str: Rich formatted trend arrow symbol.
        """
        if not isinstance(current, int) or not isinstance(previous, int):
            return ""
        if current > previous:
            return "[#ff5555]↑[/#ff5555]"
        if current < previous:
            return "[#50fa7b]↓[/#50fa7b]"
        return "[#ffb86c]→[/#ffb86c]"

    def _build_summary_table(self, results: AllResults, previous: AllResults) -> Table:
        """Build the summary table for all databases.

        Args:
            results (AllResults): Current polling results.
            previous (AllResults): Previous polling results.

        Returns:
            Table: Summary table to be rendered.
        """
        table = Table(box=SIMPLE, expand=True)

        table.add_column("Database", style="bold #8be9fd")
        table.add_column("Status", justify="center")
        table.add_column("Latency", justify="right", style="#50fa7b")
        table.add_column("1 min", justify="right")
        table.add_column("10 min", justify="right")
        table.add_column("1 hour", justify="right")

        for db_name, data in results.items():
            prev = previous.get(db_name, {})
            status = data.get("status", "OFFLINE")
            latency_ms = data.get("latency_ms")

            if status == "ONLINE":
                status_str = "[#50fa7b]● ONLINE[/#50fa7b]"
                latency_str = f"{latency_ms}ms" if latency_ms is not None else "-"
            else:
                status_str = "[#ff5555]● OFFLINE[/#ff5555]"
                latency_str = "[dim]-[/dim]"

            def fmt(val: int | str, prev_val: Any) -> str:
                arrow = self._trend_arrow(val, prev_val)
                if not isinstance(val, int):
                    return f"[#ff5555]{val}[/#ff5555]"
                if val == 0:
                    return "[dim #6272a4]-[/dim #6272a4]"
                if val > 20:
                    return f"[bold #ff5555]{val}[/bold #ff5555] {arrow}"
                return f"[#ffb86c]{val}[/#ffb86c] {arrow}"

            table.add_row(
                db_name,
                status_str,
                latency_str,
                fmt(data.get("1m", "ERR"), prev.get("1m")),
                fmt(data.get("10m", "ERR"), prev.get("10m")),
                fmt(data.get("1h", "ERR"), prev.get("1h")),
            )
        return table

    @staticmethod
    def _build_event_key(db_name: str, row: FailureDetail) -> EventKey:
        """Build a unique event key to track highlight age.

        Args:
            db_name (str): Name of the database.
            row (FailureDetail): Failed login row details.

        Returns:
            EventKey: Unique key identifying the failure event.
        """
        return (db_name, row["last_failed_at"], row["user"], row["ip"])

    def _mark_new_rows(
        self,
        db_name: str,
        rows: list[FailureDetail],
    ) -> list[tuple[FailureDetail, str]]:
        """Mark row styles based on whether they are new or aging.

        Args:
            db_name (str): Name of the database.
            rows (list[FailureDetail]): Row details to styles.

        Returns:
            list[tuple[FailureDetail, str]]: List of tuples with row and its style string.
        """
        if db_name not in self.seen_events:
            self.seen_events[db_name] = set()

        marked: list[tuple[FailureDetail, str]] = []
        for row in rows:
            key = self._build_event_key(db_name, row)
            if key not in self.seen_events[db_name]:
                self.seen_events[db_name].add(key)
                self.event_age[key] = 0
                style = "bold red"
            else:
                age = self.event_age.get(key, 0)
                if age < self.highlight_ttl:
                    style = "bold yellow"
                    self.event_age[key] = age + 1
                else:
                    style = ""
            marked.append((row, style))
        return marked

    def _build_detail_tables(self, results: AllResults) -> Group:
        """Build the panel group containing detailed tables for all databases.

        Args:
            results (AllResults): Current polling results.

        Returns:
            Group: Group of panels representing detail tables.
        """
        components: list[Any] = []
        for db_name, data in results.items():
            table = Table(
                title=f"[bold #8be9fd]{db_name}[/bold #8be9fd]",
                box=SIMPLE,
                expand=True,
            )
            table.title_align = "left"
            table.add_column("User", style="bold #ffb86c")
            table.add_column("Source IP", style="dim white")
            table.add_column("Last Failed", style="#50fa7b")
            table.add_column("1m", justify="right")
            table.add_column("10m", justify="right")
            table.add_column("1h", justify="right")

            rows = data.get("details", [])
            if rows:
                marked_rows = self._mark_new_rows(db_name, rows)
                for row, style in marked_rows:
                    if style == "bold red":
                        row_style = "bold #ff5555"
                    elif style == "bold yellow":
                        row_style = "bold #ffb86c"
                    else:
                        row_style = ""

                    table.add_row(
                        str(row["user"]),
                        str(row["ip"]),
                        str(row["last_failed_at"]),
                        str(row["1m"]),
                        str(row["10m"]),
                        str(row["1h"]),
                        style=row_style,
                    )
            else:
                table.add_row(
                    "[dim]No failures[/dim]",
                    "[dim]-[/dim]",
                    "[dim]-[/dim]",
                    "[dim]-[/dim]",
                    "[dim]-[/dim]",
                    "[dim]-[/dim]",
                )

            components.append(Panel(table, border_style="#4e5a65", box=ROUNDED))

        if not components:
            return Group(
                Align.center(
                    Text(
                        "Poller starting or no database configured...",
                        style="dim white",
                    )
                )
            )

        return Group(*components)

    def _build_layout(self, results: AllResults, previous: AllResults) -> Layout:
        """Create the dashboard layout with header, body, logs, and footer.

        Args:
            results (AllResults): Current polling results.
            previous (AllResults): Previous polling results.

        Returns:
            Layout: Rich layout containing the structured dashboard components.
        """
        layout = Layout()

        layout.split(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="logs", size=7),
            Layout(name="footer", size=1),
        )

        layout["body"].split_row(
            Layout(name="overview", ratio=4),
            Layout(name="details", ratio=6),
        )

        # Header Text
        header_text = Text()
        header_text.append(
            " 🔒 ORACLE LOGIN FAILURE MONITOR ", style="bold white on #4e5a65"
        )
        header_text.append(
            f"  Refresh: {self.app_config.refresh_seconds}s | Workers: {self.app_config.max_workers} | Highlight TTL: {self.highlight_ttl} cycles",
            style="dim white",
        )

        layout["header"].update(
            Panel(
                Align.center(header_text, vertical="middle"),
                border_style="#4e5a65",
                box=ROUNDED,
            )
        )

        # Overview Pane
        overview_table = self._build_summary_table(results, previous)
        layout["body"]["overview"].update(
            Panel(
                overview_table,
                title="[bold #8be9fd]Database Overview[/bold #8be9fd]",
                border_style="#6272a4",
                box=ROUNDED,
            )
        )

        # Details Pane
        detail_tables = self._build_detail_tables(results)
        layout["body"]["details"].update(
            Panel(
                detail_tables,
                title="[bold #8be9fd]Recent Logon Failures (Audit Trail)[/bold #8be9fd]",
                border_style="#6272a4",
                box=ROUNDED,
            )
        )

        # Logs Pane
        logs_text = Text()
        if self.log_messages:
            for idx, msg in enumerate(self.log_messages):
                if idx > 0:
                    logs_text.append("\n")
                logs_text.append(msg, style="dim white")
        else:
            logs_text.append("Waiting for logs...", style="dim white")

        layout["logs"].update(
            Panel(
                logs_text,
                title="[bold #8be9fd]System Logs[/bold #8be9fd]",
                border_style="#4e5a65",
                box=ROUNDED,
            )
        )

        # Footer Pane
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        footer_text = Text()
        footer_text.append(" Ctrl+C ", style="bold black on #8be9fd")
        footer_text.append(" Quit  ", style="bold white on #282a36")
        footer_text.append(" Last Polled: ", style="dim white")
        footer_text.append(now_str, style="bold #50fa7b")

        layout["footer"].update(Align.left(footer_text))

        return layout

    def run(self) -> None:
        """Run the live dashboard refresh loop (TUI or headless)."""
        results: AllResults = {}
        self.previous_results = {}

        logger.info("Oracle Login Failure Monitor starting...")

        if self.headless:
            try:
                while True:
                    self.previous_results = results.copy()
                    results = self._fetch_all()

                    for db_name, data in results.items():
                        status = data.get("status", "OFFLINE")
                        latency = data.get("latency_ms")
                        cnt_1m = data.get("1m")
                        cnt_10m = data.get("10m")
                        cnt_1h = data.get("1h")

                        latency_str = f"{latency}ms" if latency is not None else "-"
                        logger.info(
                            f"Database: {db_name} | Status: {status} | Latency: {latency_str} | "
                            f"Failures (1m/10m/1h): {cnt_1m}/{cnt_10m}/{cnt_1h}"
                        )

                        details = data.get("details", [])
                        if details:
                            marked = self._mark_new_rows(db_name, details)
                            for row, style in marked:
                                if style == "bold red":
                                    logger.warning(
                                        f"NEW FAILURE on {db_name} | User: {row['user']} | "
                                        f"IP: {row['ip']} | Last Failed: {row['last_failed_at']}"
                                    )

                    time.sleep(self.app_config.refresh_seconds)
            finally:
                self.close()
        else:
            try:
                with Live(
                    self._build_layout(results, self.previous_results),
                    refresh_per_second=1,
                    screen=True,
                ) as live:
                    # Run the first query immediately
                    results = self._fetch_all()
                    live.update(self._build_layout(results, self.previous_results))

                    while True:
                        self.previous_results = results.copy()
                        time.sleep(self.app_config.refresh_seconds)
                        results = self._fetch_all()
                        live.update(self._build_layout(results, self.previous_results))
            finally:
                self.close()


def main() -> None:
    """Create the monitor and start the dashboard loop."""
    parser = argparse.ArgumentParser(description="Oracle Login Failure Monitor")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run in headless daemon mode (no terminal UI)",
    )
    args = parser.parse_args()

    try:
        monitor = OracleLoginFailureMonitor(
            config_path=args.config,
            headless=args.headless,
        )
        monitor.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
