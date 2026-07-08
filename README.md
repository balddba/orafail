# Orafail

A live, terminal-based dashboard that polls multiple Oracle databases concurrently for failed logon attempts. Designed for DBAs and security administrators, it helps identify potential brute-force attacks, misconfigured connection scripts, and unauthorized access attempts in real-time.

## Features

- **Concurrent Polling:** Uses a thread pool (`ThreadPoolExecutor`) to monitor multiple Oracle databases concurrently without blocking the UI.
- **Terminal UI Dashboard:** A beautiful, responsive console interface built with [Rich](https://github.com/Textualize/rich) featuring:
  - **Live Aggregates:** Displays logon failure counts grouped by sliding time-windows: **1 minute**, **10 minutes**, and **1 hour**.
  - **Trend Indicators:** Up, down, and neutral arrows comparison to the previous polling cycle.
  - **Connection Latency:** Tracks response time (in milliseconds) for each target database.
  - **Highlight Aging:** Color-codes new failure events in **bold red** and fades them through **bold yellow** over customizable cycles before returning to normal text.
  - **Integrated Log Viewer:** Real-time log capture and display directly on the dashboard.
- **Headless Daemon Mode:** Run the monitor as a background service (`--headless`) that streams structured log entries to stdout and log files.
- **Connection Caching & Resiliency:** Caches connections, performs active ping health-checks, handles automatic reconnects, and enforces TCP connect and query execution timeouts.
- **Auditing Source:** Queries Oracle's `unified_audit_trail` (where `action_name = 'LOGON'` and `return_code != 0`).

---

## Architecture

The following diagram illustrates the flow of data from target databases to the monitor:

---

## Installation

This project is built using Python 3.13+ and managed via the [uv](https://github.com/astral-sh/uv) package manager.

1. **Clone the repository:**
   ```bash
   git clone https://github.com/balddba/orafail.git
   cd orafail
   ```

2. **Sync dependencies:**
   This automatically configures a virtual environment and installs required packages (`oracledb`, `pydantic`, `pyyaml`, `rich`, `loguru`):
   ```bash
   uv sync
   ```

---

## Configuration

Copy the example configuration to create your local config file:
```bash
cp config.example.yaml config.yaml
```

Update `config.yaml` with your connection parameters. Do not commit `config.yaml` to source control, as it contains sensitive database credentials.

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `databases` | `list` | Required | List of databases to monitor (each requiring `name`, `dsn`, `user`, and `password`). |
| `max_workers` | `int` | `5` | Maximum number of concurrent database query worker threads. |
| `refresh_seconds` | `int` | `15` | Polling frequency / UI refresh interval in seconds. |
| `highlight_ttl` | `int` | `3` | Number of cycles a new logon failure row remains highlighted. |
| `log_file` | `str` | `null` | Path to a log file for logging output. |
| `log_level` | `str` | `"INFO"` | Logging severity threshold (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `tcp_connect_timeout` | `int` | `10` | Timeout in seconds when establishing database TCP connections. |
| `query_timeout` | `int` | `10` | Timeout in seconds for executing the database audit query. |

#### Sample `config.yaml`
```yaml
databases:
  - name: prod-db
    dsn: db-prod.balddba.com:1521/orcl
    user: system
    password: "YourSecurePassword"

max_workers: 5
refresh_seconds: 15
highlight_ttl: 3
log_file: "monitor.log"
log_level: "INFO"
```

---

## Usage

You can use the bundled helper script `orafail` to run the application. The launcher will automatically prefer your `uv` environment, falling back to a virtual environment or system Python if necessary.

### Command Line Interface

```bash
./orafail --help
```

```
usage: orafail [-h] [--config CONFIG] [--headless]

Oracle Login Failure Monitor

options:
  -h, --help       show this help message and exit
  --config CONFIG  Path to the YAML configuration file (default: config.yaml)
  --headless       Run in headless daemon mode (no terminal UI)
```

### Running the Live TUI Dashboard

```bash
./orafail
```

### Running as a Headless Daemon

To run the monitor in the background or stream events to standard logging (e.g., for ingestion by SIEM tools like Splunk or ELK):

```bash
./orafail --headless
```

---

## Example Outputs

### Headless Mode Logs
When running in `--headless` mode, the monitor logs connection statuses and logon failure events to stdout/log files:

```text
2026-07-08 15:52:25.704 | INFO     | orafail.main:run:573 - Oracle Login Failure Monitor starting...
2026-07-08 15:52:25.911 | INFO     | orafail.main:run:589 - Database: prod-db | Status: ONLINE | Latency: 204ms | Failures (1m/10m/1h): 0/0/1
2026-07-08 15:52:25.911 | WARNING  | orafail.main:run:599 - NEW FAILURE on prod-db | User: STEVE | IP: oracle26ai.balddba.com | Last Failed: 2026-07-08 15:38:15.064532
2026-07-08 15:52:25.911 | WARNING  | orafail.main:run:599 - NEW FAILURE on prod-db | User: SYSTEM | IP: workstation.balddba.com | Last Failed: 2026-07-08 13:58:53.295442
2026-07-08 15:52:25.911 | WARNING  | orafail.main:run:599 - NEW FAILURE on prod-db | User: AMYERS | IP: workstation.balddba.com | Last Failed: 2026-07-08 13:54:11.958124
2026-07-08 15:52:25.911 | WARNING  | orafail.main:run:599 - NEW FAILURE on prod-db | User: SYSTEM | IP: rotation-ui.balddba.com | Last Failed: 2026-06-30 14:36:37.579003
2026-07-08 15:52:25.911 | WARNING  | orafail.main:run:599 - NEW FAILURE on prod-db | User: C##DBA_PW_ROTATION | IP: app-container.balddba.com | Last Failed: 2026-06-19 10:35:23.796769
```

---

## Development & Testing

### Running Tests

Execute the unit tests using `pytest` inside the workspace environment:

```bash
uv run --with pytest pytest
```

### Formatting & Linting

We enforce codebase standards using **Ruff**. Run the formatter and linter before submitting PRs:

```bash
# Format codebase
uv run --with ruff ruff format src/ tests/

# Check lints
uv run --with ruff ruff check src/ tests/
```

## Security Best Practices

1. **Least-Privilege Database Account:** Create a dedicated read-only database user for monitoring. The user only needs access to `SYS.UNIFIED_AUDIT_TRAIL` or equivalent audit views.
2. **Credential Management:** Avoid embedding passwords in scripts. Keep `config.yaml` securely on the system and restrict read permissions.
