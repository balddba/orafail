from pydantic import BaseModel, ConfigDict, Field

from database_config import DatabaseConfig


class AppConfig(BaseModel):
    """Define runtime configuration for the dashboard process.

    Attributes:
        databases (list[database_config.DatabaseConfig]): Database definitions to poll.
        max_workers (int): Thread pool size used for concurrent polling.
        refresh_seconds (int): Delay between polling cycles.
        highlight_ttl (int): Number of cycles to keep new-row highlighting.
    """

    model_config = ConfigDict(extra="forbid")

    databases: list[DatabaseConfig]
    max_workers: int = Field(default=5, ge=1)
    refresh_seconds: int = Field(default=15, ge=1)
    highlight_ttl: int = Field(default=3, ge=1)
