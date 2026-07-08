# Oracle Login Failure Monitor

Live terminal dashboard that polls Oracle databases for failed login events and renders them with Rich.

## Project Structure

| File | Purpose |
|------|---------|
| `oraacle_loging_failure_monitor.py` | Main entry point (note intentional typos in filename) |
| `app_config.py` | `AppConfig` Pydantic model (databases, max_workers, refresh_seconds, highlight_ttl) |
| `database_config.py` | `DatabaseConfig` Pydantic model (name, dsn, user, password) |
| `config.yaml` | Active config with real credentials — **not committed to git** |
| `config.example.yaml` | Template for new setups |
| `pyproject.toml` | Project metadata and dependencies |
| `orafail` | Bash launcher script — runs the monitor via `.venv` or system `python3` |

## Running

```bash
./orafail
# or
uv run oraacle_loging_failure_monitor.py
```

## Dependencies

Managed with **uv** (Python 3.13+):

- `oracledb` — Oracle thin-mode driver (no Oracle client required)
- `pydantic` — Config validation
- `pyyaml` — Config loading
- `rich` — Terminal dashboard rendering

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
```

**`config.yaml` contains credentials — never commit it.**

## Architecture

- `OracleLoginFailureMonitor` class wraps all state and rendering logic
- `_fetch_all()` uses `ThreadPoolExecutor` to query all databases concurrently
- Dashboard renders a summary table (counts at 1m/10m/1h windows) plus per-database detail tables
- New failure rows are highlighted red for one cycle, then yellow for `highlight_ttl` cycles, then unstyled
- Trend arrows (up/down/neutral) compare current counts to the previous poll

## Queries

Both queries target `unified_audit_trail` where `action_name = 'LOGON'` and `return_code != 0`. The detail query returns the top 5 user/host combinations by most recent failure.
