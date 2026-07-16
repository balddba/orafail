"""Event key model for the Oracle login failure monitor."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class EventKey(BaseModel):
    """Unique key representing a specific login failure event.

    Attributes:
        db_name (str): Name of the database.
        last_failed_at (Any): Timestamp of the last failed logon.
        user (str): Database username.
        ip (str): Remote/source IP address.
    """

    model_config = ConfigDict(frozen=True)

    db_name: str
    last_failed_at: Any
    user: str
    ip: str
