"""Render a live dashboard of failed Oracle login events."""

import argparse
from collections import deque
import concurrent.futures
from datetime import datetime, timedelta
from pathlib import Path
import random
import threading
import time
from typing import Any

import oracledb
import yaml
from loguru import logger
from rich.align import Align
from rich.box import ROUNDED, SIMPLE
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from orafail.config import AppConfig, DatabaseConfig
from orafail.models.failure_detail import FailureDetail
from orafail.models.database_result import DatabaseResult
from orafail.models.event_key import EventKey
from orafail.models.all_results import AllResults


DEMO_DATABASES = [
    DatabaseConfig(
        name="prod-oltp", dsn="oracle-prod:1521/prod", user="demo", password="*"
    ),
    DatabaseConfig(
        name="dw-warehouse", dsn="oracle-dw:1521/dw", user="demo", password="*"
    ),
    DatabaseConfig(
        name="auth-db", dsn="oracle-auth:1521/auth", user="demo", password="*"
    ),
]


# Summary query to aggregate failed logon events (return_code != 0)
# across three rolling windows (1 minute, 10 minutes, and 1 hour) using SYSTIMESTAMP.
SUMMARY_QUERY = """
SELECT
  SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '1' MINUTE THEN 1 ELSE 0 END),
  SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '10' MINUTE THEN 1 ELSE 0 END),
  SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '1' HOUR THEN 1 ELSE 0 END)
FROM unified_audit_trail
WHERE action_name = 'LOGON'
AND return_code != 0
"""

# Detail query to retrieve specific recent failed logons (grouped by username and host IP)
# along with rolling window counts, limited to the top 5 most recent records.
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
    def load_config(path: str = "config.yaml", demo: bool = False) -> AppConfig:
        """Load and validate dashboard configuration from YAML.

        Args:
            path (str): Path to the YAML file.
            demo (bool): True if running in demo mode with synthetic data.

        Returns:
            AppConfig: Parsed and validated application configuration.

        Raises:
            FileNotFoundError: If the YAML file path does not exist.
            ValueError: If the YAML file is empty or invalid for AppConfig.
        """
        config_path = Path(path)
        if demo and not config_path.exists():
            return AppConfig(databases=DEMO_DATABASES)

        # Ensure the configuration file exists on disk
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        # Open and load the YAML document safely
        with config_path.open("r", encoding="utf-8") as config_file:
            raw_config: dict[str, Any] | None = yaml.safe_load(config_file)

        # Validate that the file is not empty
        if raw_config is None:
            if demo:
                return AppConfig(databases=DEMO_DATABASES)
            raise ValueError(f"Config file is empty: {config_path}")

        if demo:
            raw_config["databases"] = [db.model_dump() for db in DEMO_DATABASES]

        # Parse and validate schema against Pydantic config model
        return AppConfig.model_validate(raw_config)

    def __init__(
        self,
        config_path: str = "config.yaml",
        headless: bool = False,
        demo: bool = False,
        sort_by: str | None = None,
    ) -> None:
        """Initialize monitor state by loading configuration.

        Args:
            config_path (str): Path to the YAML configuration file.
            headless (bool): Run in headless daemon mode (no terminal UI).
            demo (bool): True if running in demo mode with synthetic data.
            sort_by (str | None): Column to sort failures by.
        """
        # Load and bind core settings
        self.demo = demo
        self.app_config = self.load_config(config_path, demo=demo)
        self.databases = self.app_config.databases
        self.highlight_ttl = self.app_config.highlight_ttl
        self.sort_by = sort_by or self.app_config.sort_by

        # Initialize internal monitoring metrics and trackers
        self.previous_results = AllResults(results={})
        self.seen_events: dict[str, set[EventKey]] = {}
        self.event_age: dict[EventKey, int] = {}
        # Keep logs history of at most 5 messages for dashboard rendering
        self.log_messages: deque[str] = deque(maxlen=5)
        self.headless = headless

        # Thread safety structures for database connection cache
        self._connections: dict[str, oracledb.Connection] = {}
        self._conn_lock = threading.Lock()

        # Synthetic history for demo databases
        self._synthetic_failures: dict[str, list[dict[str, Any]]] = {}
        if self.demo:
            for db in self.databases:
                self._synthetic_failures[db.name] = []
            self._prepopulate_synthetic_history()

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

    def _prepopulate_synthetic_history(self) -> None:
        """Pre-populate the synthetic history with random historical events from the last 1 hour."""
        users = ["system", "scott", "admin", "app_user", "reporting_service", "dbsnmp"]
        ips = [
            "192.168.1.50",
            "192.168.1.100",
            "10.0.0.12",
            "10.0.0.15",
            "172.16.4.8",
            "localhost",
        ]
        now = datetime.now()

        # Seed random historical events
        for db in self.databases:
            # Generate between 5 and 15 events for each database spread over the past 1 hour (3600 seconds)
            num_events = random.randint(5, 15)
            for _ in range(num_events):
                age_seconds = random.randint(5, 3600)
                event_time = now - timedelta(seconds=age_seconds)
                self._synthetic_failures[db.name].append(
                    {
                        "user": random.choice(users),
                        "ip": random.choice(ips),
                        "last_failed_at": event_time,
                    }
                )
            # Sort events by last_failed_at ascending to make adding/sorting simpler
            self._synthetic_failures[db.name].sort(key=lambda x: x["last_failed_at"])

    def _fetch_failed_logins(self, db: DatabaseConfig) -> DatabaseResult:
        """Query one database for failed login summaries and details.

        Args:
            db (DatabaseConfig): Connection details for the target database.

        Returns:
            DatabaseResult: Summary counters and detail rows for one database.
        """
        start_time = time.perf_counter()
        if self.demo:
            # Simulate query latency by sleeping briefly (e.g. 20ms to 120ms)
            latency_sec = random.uniform(0.02, 0.12)
            time.sleep(latency_sec)
            latency_ms = int(latency_sec * 1000)

            # Keep 1 database offline occasionally (dw-warehouse has 5% chance of being offline)
            if db.name == "dw-warehouse" and random.random() < 0.05:
                return DatabaseResult(
                    status="OFFLINE",
                    latency_ms=None,
                    m1="ERR",
                    m10="ERR",
                    h1="ERR",
                    details=[],
                )

            # 25% chance of a new failed login event during a cycle
            if random.random() < 0.25:
                users = [
                    "system",
                    "scott",
                    "admin",
                    "app_user",
                    "reporting_service",
                    "dbsnmp",
                ]
                ips = [
                    "192.168.1.50",
                    "192.168.1.100",
                    "10.0.0.12",
                    "10.0.0.15",
                    "172.16.4.8",
                    "localhost",
                ]
                self._synthetic_failures[db.name].append(
                    {
                        "user": random.choice(users),
                        "ip": random.choice(ips),
                        "last_failed_at": datetime.now(),
                    }
                )

            # Filter out events older than 1 hour (3600 seconds)
            now = datetime.now()
            one_hour_ago = now - timedelta(hours=1)
            self._synthetic_failures[db.name] = [
                ev
                for ev in self._synthetic_failures[db.name]
                if ev["last_failed_at"] >= one_hour_ago
            ]

            # Compute sliding windows
            one_min_ago = now - timedelta(minutes=1)
            ten_min_ago = now - timedelta(minutes=10)

            cnt_1m = sum(
                1
                for ev in self._synthetic_failures[db.name]
                if ev["last_failed_at"] >= one_min_ago
            )
            cnt_10m = sum(
                1
                for ev in self._synthetic_failures[db.name]
                if ev["last_failed_at"] >= ten_min_ago
            )
            cnt_1h = len(self._synthetic_failures[db.name])

            # Build details (top 5 most recent events, sorted descending by timestamp)
            recent_events = sorted(
                self._synthetic_failures[db.name],
                key=lambda x: x["last_failed_at"],
                reverse=True,
            )[:5]

            # Format details for returning
            details: list[FailureDetail] = [
                FailureDetail(
                    user=ev["user"],
                    ip=ev["ip"],
                    last_failed_at=ev["last_failed_at"],
                    m1=sum(
                        1
                        for e in self._synthetic_failures[db.name]
                        if e["user"] == ev["user"]
                        and e["ip"] == ev["ip"]
                        and e["last_failed_at"] >= one_min_ago
                    ),
                    m10=sum(
                        1
                        for e in self._synthetic_failures[db.name]
                        if e["user"] == ev["user"]
                        and e["ip"] == ev["ip"]
                        and e["last_failed_at"] >= ten_min_ago
                    ),
                    h1=sum(
                        1
                        for e in self._synthetic_failures[db.name]
                        if e["user"] == ev["user"] and e["ip"] == ev["ip"]
                    ),
                )
                for ev in recent_events
            ]

            return DatabaseResult(
                status="ONLINE",
                latency_ms=latency_ms,
                m1=cnt_1m,
                m10=cnt_10m,
                h1=cnt_1h,
                details=details,
            )

        conn = None
        try:
            # Thread-safe retrieval and validation of connection from local cache
            with self._conn_lock:
                conn = self._connections.get(db.name)
                if conn is not None:
                    try:
                        # Verify the connection remains alive
                        conn.ping()
                    except Exception:
                        # Clear broken connections
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None

                # Establish a new connection if not cached or cached connection is dead
                if conn is None:
                    conn = oracledb.connect(
                        user=db.user,
                        password=db.password.get_secret_value(),
                        dsn=db.dsn,
                        tcp_connect_timeout=self.app_config.tcp_connect_timeout,
                    )
                    self._connections[db.name] = conn

            # Initialize cursor to execute DB queries
            cursor = conn.cursor()

            # Execute the summary count aggregation
            cursor.execute(SUMMARY_QUERY)
            summary_row = cursor.fetchone()

            # Execute the detailed logon failures query
            cursor.execute(DETAIL_QUERY)
            detail_rows = cursor.fetchall()

            # Map raw tuples to a list of structured Pydantic models
            details: list[FailureDetail] = [
                FailureDetail(
                    user=row[0],
                    ip=row[1],
                    last_failed_at=row[2],
                    m1=row[3] or 0,
                    m10=row[4] or 0,
                    h1=row[5] or 0,
                )
                for row in detail_rows
            ]

            cursor.close()

            # Compute round-trip query time
            latency_ms = int((time.perf_counter() - start_time) * 1000)

            return DatabaseResult(
                status="ONLINE",
                latency_ms=latency_ms,
                m1=summary_row[0] or 0,
                m10=summary_row[1] or 0,
                h1=summary_row[2] or 0,
                details=details,
            )
        except Exception as e:
            # Clean up broken connection state and return OFFLINE status
            logger.error(f"Failed to query database {db.name}: {e}")
            with self._conn_lock:
                if db.name in self._connections:
                    try:
                        self._connections[db.name].close()
                    except Exception:
                        pass
                    del self._connections[db.name]
            return DatabaseResult(
                status="OFFLINE",
                latency_ms=None,
                m1="ERR",
                m10="ERR",
                h1="ERR",
                details=[],
            )

    def close(self) -> None:
        """Close all cached database connections."""
        with self._conn_lock:
            # Iterate through connections dictionary and safely close each connection
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
        # Execute queries in parallel using thread pool workers to minimize refresh times
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.app_config.max_workers
        ) as executor:
            futures = {
                executor.submit(self._fetch_failed_logins, db): db
                for db in self.databases
            }

            # Wait for all tasks to complete, or abort if the global timeout is exceeded
            done, not_done = concurrent.futures.wait(
                futures.keys(),
                timeout=float(self.app_config.query_timeout),
            )

            results = AllResults(results={})

            # Process successfully completed queries
            for future in done:
                db = futures[future]
                try:
                    results.results[db.name] = future.result()
                except Exception as e:
                    logger.error(f"Error fetching logs for {db.name}: {e}")
                    results.results[db.name] = DatabaseResult(
                        status="OFFLINE",
                        latency_ms=None,
                        m1="ERR",
                        m10="ERR",
                        h1="ERR",
                        details=[],
                    )

            # Process timed out queries and label them as TIMEOUT
            for future in not_done:
                db = futures[future]
                logger.warning(
                    f"Query timed out for database {db.name} after {self.app_config.query_timeout}s"
                )
                results.results[db.name] = DatabaseResult(
                    status="OFFLINE",
                    latency_ms=None,
                    m1="TIMEOUT",
                    m10="TIMEOUT",
                    h1="TIMEOUT",
                    details=[],
                )

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
        # If there is no previous run to compare against, do not display trend arrows
        if not isinstance(current, int) or not isinstance(previous, int):
            return ""
        # Upwards arrow: counts are rising
        if current > previous:
            return "[#f38ba8]↑[/#f38ba8]"
        # Downwards arrow: counts are falling
        if current < previous:
            return "[#a6e3a1]↓[/#a6e3a1]"
        # Sideways arrow: counts remain unchanged
        return "[#f9e2af]→[/#f9e2af]"

    def _build_summary_table(self, results: AllResults, previous: AllResults) -> Table:
        """Build the summary table for all databases.

        Args:
            results (AllResults): Current polling results.
            previous (AllResults): Previous polling results.

        Returns:
            Table: Summary table to be rendered.
        """
        # Create a new rich Table with styled columns
        table = Table(box=SIMPLE, expand=True)

        table.add_column("Database", style="bold", no_wrap=True)
        table.add_column("Status", justify="center", no_wrap=True)
        table.add_column("Latency", justify="right", style="#a6e3a1", no_wrap=True)
        table.add_column("1 min", justify="right", no_wrap=True)
        table.add_column("10 min", justify="right", no_wrap=True)
        table.add_column("1 hour", justify="right", no_wrap=True)

        for db_name, data in results.items():
            prev = previous.get(db_name)
            status = data.status
            latency_ms = data.latency_ms

            # Style status markers based on connectivity
            if status == "ONLINE":
                status_str = "[#a6e3a1]● ONLINE[/#a6e3a1]"
                latency_str = f"{latency_ms}ms" if latency_ms is not None else "-"
            else:
                status_str = "[#f38ba8]● OFFLINE[/#f38ba8]"
                latency_str = "[dim]-[/dim]"

            # Format cell content with color-coded severity levels
            def fmt(val: int | str, prev_val: Any) -> str:
                arrow = self._trend_arrow(val, prev_val)
                if not isinstance(val, int):
                    return f"[#f38ba8]{val}[/#f38ba8]"
                if val == 0:
                    return "[dim #585b70]-[/dim #585b70]"
                if val > 20:
                    return f"[bold #f5c2e7]{val}[/bold #f5c2e7] {arrow}"
                return f"[#fab387]{val}[/#fab387] {arrow}"

            prev_1m = prev.m1 if prev else None
            prev_10m = prev.m10 if prev else None
            prev_1h = prev.h1 if prev else None

            db_color = self._get_db_color(db_name)
            table.add_row(
                f"[bold {db_color}]{db_name}[/bold {db_color}]",
                status_str,
                latency_str,
                fmt(data.m1, prev_1m),
                fmt(data.m10, prev_10m),
                fmt(data.h1, prev_1h),
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
        # Composite key consisting of database identifier, event timestamp, username, and remote IP
        return (db_name, row.last_failed_at, row.user, row.ip)

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
            # If the event is seen for the first time, color it bold red and add to seen set
            if key not in self.seen_events[db_name]:
                self.seen_events[db_name].add(key)
                self.event_age[key] = 0
                style = "bold red"
            else:
                # If the event is already known, age the highlight timer until it exceeds highlight_ttl
                age = self.event_age.get(key, 0)
                if age < self.highlight_ttl:
                    style = "bold yellow"
                    self.event_age[key] = age + 1
                else:
                    style = ""
            marked.append((row, style))
        return marked

    def _get_db_color(self, db_name: str) -> str:
        """Get the unique assigned color style for a database name.

        Args:
            db_name (str): The database name.

        Returns:
            str: Color code in hex.
        """
        if not hasattr(self, "db_colors"):
            self.db_colors = {}
        if db_name not in self.db_colors:
            colors = ["#89b4fa", "#cba6f7", "#89dceb", "#f5c2e7", "#b4befe", "#94e2d5"]
            used_colors = set(self.db_colors.values())
            unused_colors = [c for c in colors if c not in used_colors]
            if unused_colors:
                self.db_colors[db_name] = unused_colors[0]
            else:
                self.db_colors[db_name] = colors[len(self.db_colors) % len(colors)]
        return self.db_colors[db_name]

    @staticmethod
    def _get_user_color(username: str) -> str:
        """Get a deterministic color style for a username.

        Args:
            username (str): The username.

        Returns:
            str: Color code in hex.
        """
        # Cycle through warm/pastel colors for usernames
        colors = [
            "#f5e0dc",
            "#f2cdcd",
            "#fab387",
            "#f9e2af",
            "#cba6f7",
            "#b4befe",
            "#f5c2e7",
        ]
        idx = sum(ord(c) for c in username) % len(colors)
        return colors[idx]

    def _build_detail_tables(self, results: AllResults) -> Table:
        """Build a single detailed table containing failed logins from all databases.

        Args:
            results (AllResults): Current polling results.

        Returns:
            Table: Unified table showing recent failed login events.
        """
        table = Table(box=SIMPLE, expand=True)
        table.add_column("Database", style="bold", no_wrap=True)
        table.add_column("User", style="bold", no_wrap=True)
        table.add_column("Source IP", style="#a6adc8", no_wrap=True)
        table.add_column("Last Failed", no_wrap=True)
        table.add_column("1m", justify="right", no_wrap=True)
        table.add_column("10m", justify="right", no_wrap=True)
        table.add_column("1h", justify="right", no_wrap=True)

        if not results:
            table.add_row(
                "[dim]Poller starting or no database configured...[/dim]",
                "",
                "",
                "",
                "",
                "",
                "",
            )
            return table

        all_failed_rows = []
        for db_name, data in results.items():
            rows = data.details
            if rows:
                marked_rows = self._mark_new_rows(db_name, rows)
                for row, style in marked_rows:
                    all_failed_rows.append((db_name, row, style))

        reverse = True
        if self.sort_by == "user":
            def get_sort_key(item: tuple[str, FailureDetail, str]) -> Any:
                ts = item[1].last_failed_at.timestamp() if isinstance(item[1].last_failed_at, datetime) else 0.0
                return (item[1].user.lower() if item[1].user else "", -ts)
            reverse = False
        elif self.sort_by == "database":
            def get_sort_key(item: tuple[str, FailureDetail, str]) -> Any:
                ts = item[1].last_failed_at.timestamp() if isinstance(item[1].last_failed_at, datetime) else 0.0
                return (item[0].lower(), -ts)
            reverse = False
        elif self.sort_by == "count":
            def get_sort_key(item: tuple[str, FailureDetail, str]) -> Any:
                ts = item[1].last_failed_at.timestamp() if isinstance(item[1].last_failed_at, datetime) else 0.0
                return (item[1].m1 or 0, ts)
            reverse = True
        else:  # default "time"
            def get_sort_key(item: tuple[str, FailureDetail, str]) -> Any:
                val = item[1].last_failed_at
                if val is None:
                    return datetime.min
                return val
            reverse = True

        all_failed_rows.sort(key=get_sort_key, reverse=reverse)

        if all_failed_rows:
            for db_name, row, style in all_failed_rows:
                # Translate age state styles to exact theme colors
                if style == "bold red":
                    last_failed_style = "bold #f5c2e7"
                elif style == "bold yellow":
                    last_failed_style = "dim #f5c2e7"
                else:
                    last_failed_style = "dim #a6adc8"

                db_color = self._get_db_color(db_name)
                user_color = self._get_user_color(row.user)
                table.add_row(
                    f"[bold {db_color}]{db_name}[/bold {db_color}]",
                    f"[bold {user_color}]{row.user}[/bold {user_color}]",
                    str(row.ip),
                    f"[{last_failed_style}]{row.last_failed_at}[/{last_failed_style}]",
                    str(row.m1),
                    str(row.m10),
                    str(row.h1),
                )
        else:
            table.add_row(
                "[dim]No failures[/dim]",
                "[dim]-[/dim]",
                "[dim]-[/dim]",
                "[dim]-[/dim]",
                "[dim]-[/dim]",
                "[dim]-[/dim]",
                "[dim]-[/dim]",
            )

        return table

    def _build_layout(self, results: AllResults, previous: AllResults) -> Layout:
        """Create the dashboard layout with header, body, logs, and footer.

        Args:
            results (AllResults): Current polling results.
            previous (AllResults): Previous polling results.

        Returns:
            Layout: Rich layout containing the structured dashboard components.
        """
        layout = Layout()

        # Split screen vertically into main sections
        layout.split(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="logs", size=7),
            Layout(name="footer", size=2),
        )

        # Split main body vertically into Overview (top) and Details (bottom) panels
        layout["body"].split(
            Layout(name="overview", ratio=1),
            Layout(name="details", ratio=2),
        )

        # Header Text
        header_text = Text()
        header_text.append(
            " 🔒 ORACLE LOGIN FAILURE MONITOR ", style="bold #11111b on #89b4fa"
        )
        header_text.append(
            f"  Ref: {self.app_config.refresh_seconds}s | Workers: {self.app_config.max_workers} | TTL: {self.highlight_ttl} cycles",
            style="#a6adc8",
        )

        layout["header"].update(
            Panel(
                Align.center(header_text, vertical="middle"),
                border_style="#45475a",
                box=ROUNDED,
            )
        )

        # Overview Pane
        overview_table = self._build_summary_table(results, previous)
        layout["body"]["overview"].update(
            Panel(
                overview_table,
                title="[bold #89b4fa]Database Overview[/bold #89b4fa]",
                border_style="#585b70",
                box=ROUNDED,
            )
        )

        # Details Pane
        detail_tables = self._build_detail_tables(results)
        layout["body"]["details"].update(
            Panel(
                detail_tables,
                title="[bold #89b4fa]Recent Logon Failures (Audit Trail)[/bold #89b4fa]",
                border_style="#585b70",
                box=ROUNDED,
            )
        )

        # Logs Pane
        logs_text = Text()
        if self.log_messages:
            for idx, msg in enumerate(self.log_messages):
                if idx > 0:
                    logs_text.append("\n")
                logs_text.append(msg, style="#a6adc8")
        else:
            logs_text.append("Waiting for logs...", style="#a6adc8")

        layout["logs"].update(
            Panel(
                logs_text,
                title="[bold #89b4fa]System Logs[/bold #89b4fa]",
                border_style="#45475a",
                box=ROUNDED,
            )
        )

        # Footer Pane
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        footer_text = Text()
        footer_text.append(" Ctrl+C ", style="bold #11111b on #b4befe")
        footer_text.append(" Quit  ", style="bold #cdd6f4 on #313244")
        footer_text.append(" Last Polled: ", style="#a6adc8")
        footer_text.append(now_str, style="bold #a6e3a1")
        footer_text.append("\n")

        footer_text.append(" Legend: ", style="#a6adc8")
        footer_text.append("●", style="#a6e3a1")
        footer_text.append(" Online  ", style="#a6adc8")
        footer_text.append("●", style="#f38ba8")
        footer_text.append(" Offline  ", style="#a6adc8")
        footer_text.append("●", style="#f5c2e7")
        footer_text.append(" New  ", style="#a6adc8")
        footer_text.append("●", style="dim #f5c2e7")
        footer_text.append(" Aging  ", style="#a6adc8")
        footer_text.append("●", style="#fab387")
        footer_text.append(" Warning", style="#a6adc8")

        layout["footer"].update(Align.left(footer_text))

        return layout

    def run(self) -> None:
        """Run the live dashboard refresh loop (TUI or headless)."""
        results: AllResults = {}
        self.previous_results = {}

        logger.info("Oracle Login Failure Monitor starting...")

        if self.headless:
            try:
                # Continuous loop for daemon/headless mode
                while True:
                    self.previous_results = results.copy()
                    results = self._fetch_all()

                    # Write connection status summaries to console/file logs
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
                            # Log any newly surfaced failures with WARNING severity
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
                # Terminal UI mode: open and refresh the Rich Live context screen
                with Live(
                    self._build_layout(results, self.previous_results),
                    refresh_per_second=1,
                    screen=True,
                ) as live:
                    # Run the first query immediately on launch
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
    # Define CLI parameters
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
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run in demo mode with synthetic data (no database connection required)",
    )
    parser.add_argument(
        "--sort-by",
        choices=["time", "user", "database", "count"],
        default=None,
        help="Column to sort failures by (default: time)",
    )
    args = parser.parse_args()

    try:
        # Instantiate and initiate monitor lifecycle
        monitor = OracleLoginFailureMonitor(
            config_path=args.config,
            headless=args.headless,
            demo=args.demo,
            sort_by=args.sort_by,
        )
        monitor.run()
    except KeyboardInterrupt:
        # Gracefully swallow KeyboardInterrupt on exit
        pass


if __name__ == "__main__":
    main()
