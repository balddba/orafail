"""Configuration models for the Oracle login failure monitor."""

from pydantic import BaseModel, ConfigDict, Field


class DatabaseConfig(BaseModel):
    """Represent a single Oracle database connection target.

    Attributes:
        name (str): Human-friendly display name for the dashboard.
        dsn (str): Oracle DSN used by the connector.
        user (str): Database username.
        password (str): Database user password.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str
    dsn: str
    user: str
    password: str


class AppConfig(BaseModel):
    """Define runtime configuration for the dashboard process.

    Attributes:
        databases (list[DatabaseConfig]): Database definitions to poll.
        max_workers (int): Thread pool size used for concurrent polling.
        refresh_seconds (int): Delay between polling cycles.
        highlight_ttl (int): Number of cycles to keep new-row highlighting.
        log_file (str | None): Log file path.
        log_level (str): Logging level.
        tcp_connect_timeout (int): Timeout in seconds to establish TCP connection.
        query_timeout (int): Timeout in seconds to wait for polling queries.
    """

    model_config = ConfigDict(extra="forbid")

    databases: list[DatabaseConfig]
    max_workers: int = Field(default=5, ge=1)
    refresh_seconds: int = Field(default=15, ge=1)
    highlight_ttl: int = Field(default=3, ge=1)
    log_file: str | None = Field(default=None)
    log_level: str = Field(default="INFO")
    tcp_connect_timeout: int = Field(default=10, ge=1)
    query_timeout: int = Field(default=10, ge=1)
