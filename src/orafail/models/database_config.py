"""Database configuration model for the Oracle login failure monitor."""

from pydantic import BaseModel, ConfigDict, SecretStr


class DatabaseConfig(BaseModel):
    """Represent a single Oracle database connection target.

    Attributes:
        name (str): Human-friendly display name for the dashboard.
        dsn (str): Oracle DSN used by the connector.
        user (str): Database username.
        password (SecretStr): Database user password.
    """

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, validate_assignment=True
    )

    name: str
    dsn: str
    user: str
    password: SecretStr
