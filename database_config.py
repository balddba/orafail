from pydantic import BaseModel, ConfigDict


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
