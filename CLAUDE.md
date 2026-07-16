# Orafail

Live terminal dashboard that polls Oracle databases for failed login events and renders them with Rich.

## Project Structure

| File | Purpose |
|------|---------|
| `src/orafail/main.py` | Main entry point, `OracleLoginFailureMonitor`, dashboard runner |
| `src/orafail/config.py` | Re-exports `AppConfig` and `DatabaseConfig` from `models/` |
| `src/orafail/models/app_config.py` | `AppConfig` — runtime settings (workers, refresh, timeouts, sort) |
| `src/orafail/models/database_config.py` | `DatabaseConfig` — per-database connection target |
| `src/orafail/models/failure_detail.py` | `FailureDetail` — single failed logon row |
| `src/orafail/models/database_result.py` | `DatabaseResult` — per-database poll result with details |
| `src/orafail/models/all_results.py` | `AllResults` — map of database name to `DatabaseResult` |
| `src/orafail/models/event_key.py` | `EventKey` — unique key for highlight tracking |
| `src/orafail/__init__.py` | Package init and `__version__` |
| `config.yaml` | Active config with real credentials — **not committed to git** |
| `config.example.yaml` | Template for new setups |
| `scripts/publish.py` | Release automation (version bump, GitHub release, PyPI publish) |
| `pyproject.toml` | Project metadata and dependencies |
| `orafail` | Bash launcher script — runs the monitor via `.venv` or system `python3` |

## Running

```bash
./orafail
# or
uv run orafail
```

### CLI Flags

| Flag | Description |
|------|-------------|
| `--config PATH` | YAML config path (default: `config.yaml`) |
| `--headless` | Daemon mode — structured logs to stdout/file, no TUI |
| `--demo` | Synthetic data mode — no Oracle connection required |
| `--sort-by {time,user,database,count}` | Sort column for the unified failure table |

## Dependencies

Managed with **uv** (Python 3.13+):

- `oracledb` — Oracle thin-mode driver (no Oracle client required)
- `pydantic` — Config and result model validation
- `pyyaml` — Config loading
- `rich` — Terminal dashboard rendering
- `loguru` — Structured logging

Install: `uv sync`

## Configuration

Copy `config.example.yaml` to `config.yaml` and fill in real values. The config supports multiple databases, each requiring `name`, `dsn`, `user`, and `password`.

```yaml
databases:
  - name: my-db
    dsn: host:1521/SERVICE
    user: monitor_user
    password: secret
max_workers: 5
refresh_seconds: 15
highlight_ttl: 3
sort_by: time
tcp_connect_timeout: 10
query_timeout: 10
```

**`config.yaml` contains credentials — never commit it.**

## Architecture

- `OracleLoginFailureMonitor` class wraps all state and rendering logic
- Config and poll results use Pydantic models in `src/orafail/models/`
- `_fetch_all()` uses `ThreadPoolExecutor` to query all databases concurrently
- Dashboard renders a summary table (counts at 1m/10m/1h windows) plus a unified failure detail table
- New failure rows are highlighted red for one cycle, then yellow for `highlight_ttl` cycles, then unstyled
- Trend arrows (up/down/neutral) compare current counts to the previous poll
- `EventKey` tracks seen events for highlight aging across refresh cycles
- Demo mode (`--demo`) substitutes three synthetic databases and generates randomized failures

## Queries

Both queries target `unified_audit_trail` where `action_name = 'LOGON'` and `return_code != 0`. The detail query returns the top 5 user/host combinations by most recent failure.
