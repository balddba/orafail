from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orafail.failure_detail import FailureDetail


class DatabaseResult(BaseModel):
    """Aggregation counters and detailed list of failure events for a database.

    Attributes:
        status (str): Current status of the database (ONLINE, OFFLINE).
        latency_ms (int | None): Latency in milliseconds, or None if offline.
        m1 (int | str): Failure count/status in the last 1 minute.
        m10 (int | str): Failure count/status in the last 10 minutes.
        h1 (int | str): Failure count/status in the last 1 hour.
        details (list[orafail.failure_detail.FailureDetail]): List of details for recent failed logins.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    status: str
    latency_ms: int | None
    m1: int | str = Field(alias="1m")
    m10: int | str = Field(alias="10m")
    h1: int | str = Field(alias="1h")
    details: list[FailureDetail]

    def __getitem__(self, item: str) -> Any:
        """Allow dict-like access for backwards compatibility.

        Args:
            item (str): Field name or alias.

        Returns:
            Any: Value of the field.

        Raises:
            KeyError: If the field does not exist.
        """
        mapping = {"1m": "m1", "10m": "m10", "1h": "h1"}
        attr = mapping.get(item, item)
        if attr in self.__class__.model_fields:
            return getattr(self, attr)
        raise KeyError(item)

    def get(self, key: str, default: Any = None) -> Any:
        """Allow dict-like get access.

        Args:
            key (str): Field name or alias.
            default (Any): Default value if key is not found.

        Returns:
            Any: Value of the field or default.
        """
        try:
            return self[key]
        except KeyError:
            return default
