"""Failure detail model for the Oracle login failure monitor."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FailureDetail(BaseModel):
    """Detailed information for a single logon failure event.

    Attributes:
        user (str): Database username.
        ip (str): Remote/source IP address.
        last_failed_at (Any): Timestamp of the last failed logon.
        m1 (int): Failure count in the last 1 minute.
        m10 (int): Failure count in the last 10 minutes.
        h1 (int): Failure count in the last 1 hour.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    user: str
    ip: str
    last_failed_at: Any
    m1: int = Field(alias="1m")
    m10: int = Field(alias="10m")
    h1: int = Field(alias="1h")

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
