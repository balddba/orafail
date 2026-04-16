"""Render a live dashboard of failed Oracle login events."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, TypeAlias

import oracledb
import yaml
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from app_config import AppConfig
from database_config import DatabaseConfig

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


def load_app_config(path: str = "config.yaml") -> AppConfig:
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

    def __init__(self, app_config: AppConfig) -> None:
        """Initialize monitor state from application configuration.

        Args:
            app_config (AppConfig): Parsed application configuration.
        """

        self.app_config = app_config
        self.databases = app_config.databases
        self.highlight_ttl = app_config.highlight_ttl
        self.previous_results: AllResults = {}
        self.seen_events: dict[str, set[EventKey]] = {}
        self.event_age: dict[EventKey, int] = {}

    def _fetch_failed_logins(self, db: DatabaseConfig) -> DatabaseResult:
        """Query one database for failed login summaries and details.

        Args:
            db (DatabaseConfig): Connection details for the target database.

        Returns:
            DatabaseResult: Summary counters and detail rows for one database.
        """

        try:
            conn = oracledb.connect(user=db.user, password=db.password, dsn=db.dsn)
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
            conn.close()

            return {
                "1m": summary_row[0] or 0,
                "10m": summary_row[1] or 0,
                "1h": summary_row[2] or 0,
                "details": details,
            }
        except Exception:
            return {"1m": "ERR", "10m": "ERR", "1h": "ERR", "details": []}

    def _fetch_all(self) -> AllResults:
        """Fetch failed-login data for all configured databases.

        Returns:
            AllResults: Per-database query results for the dashboard.
        """

        with ThreadPoolExecutor(max_workers=self.app_config.max_workers) as executor:
            futures = {
                executor.submit(self._fetch_failed_logins, db): db.name
                for db in self.databases
            }
            results: AllResults = {}
            for future in futures:
                db_name = futures[future]
                results[db_name] = future.result()
            return results

    @staticmethod
    def _trend_arrow(current: Any, previous: Any) -> str:
        if not isinstance(current, int) or not isinstance(previous, int):
            return ""
        if current > previous:
            return "[red]↑[/red]"
        if current < previous:
            return "[green]↓[/green]"
        return "[yellow]→[/yellow]"

    def _build_summary_table(self, results: AllResults, previous: AllResults) -> Table:
        table = Table(title="Oracle Failed Logins")

        table.add_column("Database", style="cyan")
        table.add_column("1 min", justify="right")
        table.add_column("10 min", justify="right")
        table.add_column("1 hour", justify="right")
        table.add_column("Updated", style="green")

        now = datetime.now().strftime("%H:%M:%S")

        for db, data in results.items():
            prev = previous.get(db, {})

            def fmt(val: int | str, prev_val: Any) -> str:
                arrow = self._trend_arrow(val, prev_val)
                if isinstance(val, int) and val > 20:
                    return f"[bold red]{val}[/bold red] {arrow}"
                return f"{val} {arrow}"

            table.add_row(
                db,
                fmt(data["1m"], prev.get("1m")),
                fmt(data["10m"], prev.get("10m")),
                fmt(data["1h"], prev.get("1h")),
                now,
            )
        return table

    @staticmethod
    def _build_event_key(db_name: str, row: FailureDetail) -> EventKey:
        return (db_name, row["last_failed_at"], row["user"], row["ip"])

    def _mark_new_rows(
        self,
        db_name: str,
        rows: list[FailureDetail],
    ) -> list[tuple[FailureDetail, str]]:
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
        panels: list[Panel] = []
        for db_name, data in results.items():
            table = Table(title=f"{db_name} - Last Failed Login by User/Source")
            table.add_column("User", style="magenta")
            table.add_column("Source IP", style="yellow")
            table.add_column("Last Failed", style="green")
            table.add_column("1m", justify="right")
            table.add_column("10m", justify="right")
            table.add_column("1h", justify="right")

            rows = data["details"]
            if rows:
                marked_rows = self._mark_new_rows(db_name, rows)
                for row, style in marked_rows:
                    table.add_row(
                        str(row["user"]),
                        str(row["ip"]),
                        str(row["last_failed_at"]),
                        str(row["1m"]),
                        str(row["10m"]),
                        str(row["1h"]),
                        style=style,
                    )
            else:
                table.add_row("-", "-", "-", "-", "-", "-")

            panels.append(Panel(table, border_style="blue"))

        return Group(*panels)

    def _build_dashboard(self, results: AllResults, previous: AllResults) -> Group:
        main_table = self._build_summary_table(results, previous)
        detail_tables = self._build_detail_tables(results)
        return Group(main_table, detail_tables)

    def run(self) -> None:
        """Run the live dashboard refresh loop."""

        results: AllResults = {}
        self.previous_results = {}

        with Live(
            self._build_dashboard(results, self.previous_results),
            refresh_per_second=1,
        ) as live:
            while True:
                self.previous_results = results.copy()
                results = self._fetch_all()
                live.update(self._build_dashboard(results, self.previous_results))
                time.sleep(self.app_config.refresh_seconds)


def main() -> None:
    """Create the monitor and start the dashboard loop."""

    app_config = load_app_config()
    monitor = OracleLoginFailureMonitor(app_config)
    monitor.run()


if __name__ == "__main__":
    main()
"""Render a live dashboard of failed Oracle login events."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import oracledb
import yaml
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from app_config import AppConfig
from database_config import DatabaseConfig


# ---------------- CONFIG ----------------


def load_app_config(path: str = "config.yaml") -> AppConfig:
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


APP_CONFIG = load_app_config()
DATABASES = APP_CONFIG.databases

# Summary counts (1m, 10m, 1h)
QUERY = """
SELECT
  SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '1' MINUTE THEN 1 ELSE 0 END),
  SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '10' MINUTE THEN 1 ELSE 0 END),
  SUM(CASE WHEN event_timestamp > SYSTIMESTAMP - INTERVAL '1' HOUR THEN 1 ELSE 0 END)
FROM unified_audit_trail
WHERE action_name = 'LOGON'
AND return_code != 0
"""

# Last 5 failed-login user/source combinations per DB
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

# ---------------- GLOBAL STATE ----------------
previous_results = {}
seen_events = {}
event_age = {}
HIGHLIGHT_TTL = APP_CONFIG.highlight_ttl  # cycles for fade-out

# ---------------- HELPERS ----------------
def get_failed_logins(db: DatabaseConfig):
    """Query one database for failed login summaries and details.

    Args:
        db (DatabaseConfig): Connection details for the target database.

    Returns:
        dict[str, int | str | list[dict[str, Any]]]: Summary counters and detail rows.
    """

    try:
        # Connect using thin mode (no Oracle client needed)
        conn = oracledb.connect(user=db.user, password=db.password, dsn=db.dsn)
        cursor = conn.cursor()

        # Summary
        cursor.execute(QUERY)
        summary_row = cursor.fetchone()

        # Details
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
        conn.close()

        return {
            "1m": summary_row[0] or 0,
            "10m": summary_row[1] or 0,
            "1h": summary_row[2] or 0,
            "details": details
        }

    except Exception:
        return {"1m": "ERR", "10m": "ERR", "1h": "ERR", "details": []}


def fetch_all():
    """Fetch failed-login data for all configured databases.

    Returns:
        dict[str, dict[str, int | str | list[dict[str, Any]]]]: Per-database query results.
    """

    with ThreadPoolExecutor(max_workers=APP_CONFIG.max_workers) as executor:
        futures = {executor.submit(get_failed_logins, db): db.name for db in DATABASES}
        results = {}
        for future in futures:
            db_name = futures[future]
            results[db_name] = future.result()
        return results


def trend_arrow(current, previous):
    if not isinstance(current, int) or not isinstance(previous, int):
        return ""
    if current > previous:
        return "[red]↑[/red]"
    elif current < previous:
        return "[green]↓[/green]"
    else:
        return "[yellow]→[/yellow]"


def build_table(results, previous):
    table = Table(title="Oracle Failed Logins")

    table.add_column("Database", style="cyan")
    table.add_column("1 min", justify="right")
    table.add_column("10 min", justify="right")
    table.add_column("1 hour", justify="right")
    table.add_column("Updated", style="green")

    now = datetime.now().strftime("%H:%M:%S")

    for db, data in results.items():
        prev = previous.get(db, {})

        def fmt(val, prev_val):
            arrow = trend_arrow(val, prev_val)
            if isinstance(val, int) and val > 20:
                return f"[bold red]{val}[/bold red] {arrow}"
            return f"{val} {arrow}"

        table.add_row(
            db,
            fmt(data["1m"], prev.get("1m")),
            fmt(data["10m"], prev.get("10m")),
            fmt(data["1h"], prev.get("1h")),
            now
        )
    return table


def event_key(db, row):
    return (db, row["last_failed_at"], row["user"], row["ip"])


def mark_new_rows(db, rows):
    if db not in seen_events:
        seen_events[db] = set()

    marked = []
    for row in rows:
        key = event_key(db, row)
        if key not in seen_events[db]:
            seen_events[db].add(key)
            event_age[key] = 0
            style = "bold red"
        else:
            age = event_age.get(key, 0)
            if age < HIGHLIGHT_TTL:
                style = "bold yellow"
                event_age[key] = age + 1
            else:
                style = ""
        marked.append((row, style))
    return marked


def build_detail_tables(results):
    panels = []
    for db, data in results.items():
        table = Table(title=f"{db} - Last Failed Login by User/Source")
        table.add_column("User", style="magenta")
        table.add_column("Source IP", style="yellow")
        table.add_column("Last Failed", style="green")
        table.add_column("1m", justify="right")
        table.add_column("10m", justify="right")
        table.add_column("1h", justify="right")

        rows = data["details"]
        if rows:
            marked_rows = mark_new_rows(db, rows)
            for row, style in marked_rows:
                table.add_row(
                    str(row["user"]),
                    str(row["ip"]),
                    str(row["last_failed_at"]),
                    str(row["1m"]),
                    str(row["10m"]),
                    str(row["1h"]),
                    style=style,
                )
        else:
            table.add_row("-", "-", "-", "-", "-", "-")

        panels.append(Panel(table, border_style="blue"))

    return Group(*panels)


def build_dashboard(results, previous):
    main_table = build_table(results, previous)
    detail_tables = build_detail_tables(results)
    return Group(main_table, detail_tables)


# ---------------- MAIN LOOP ----------------
def main():
    """Run the live dashboard refresh loop."""

    global previous_results
    results = {}
    previous_results = {}

    with Live(build_dashboard(results, previous_results), refresh_per_second=1) as live:
        while True:
            previous_results = results.copy()
            results = fetch_all()
            live.update(build_dashboard(results, previous_results))
            time.sleep(APP_CONFIG.refresh_seconds)


if __name__ == "__main__":
    main()