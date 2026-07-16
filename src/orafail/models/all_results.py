"""All results model for the Oracle login failure monitor."""

from typing import Any

from pydantic import BaseModel, ConfigDict

from orafail.models.database_result import DatabaseResult


class AllResults(BaseModel):
    """Container for polling results across all configured databases.

    Attributes:
        results (dict[str, DatabaseResult]): Database results mapped by name.
    """

    model_config = ConfigDict(extra="forbid")

    results: dict[str, DatabaseResult]

    def __getitem__(self, item: str) -> DatabaseResult:
        """Allow subscript access to database results.

        Args:
            item (str): The database name.

        Returns:
            DatabaseResult: The database query results.
        """
        return self.results[item]

    def get(self, key: str, default: Any = None) -> Any:
        """Allow dict-like get access.

        Args:
            key (str): The database name.
            default (Any): Default value if not found.

        Returns:
            DatabaseResult | None: The database query results or default.
        """
        return self.results.get(key, default)

    def items(self):
        """Allow iterating over results.

        Returns:
            dict_items: Key-value iterator of database name to DatabaseResult.
        """
        return self.results.items()

    def keys(self):
        """Allow iterating over database names.

        Returns:
            dict_keys: Iterator of database names.
        """
        return self.results.keys()

    def values(self):
        """Allow iterating over database results.

        Returns:
            dict_values: Iterator of database results.
        """
        return self.results.values()

    def __contains__(self, item: str) -> bool:
        """Check if a database exists in the results.

        Args:
            item (str): The database name.

        Returns:
            bool: True if database exists, False otherwise.
        """
        return item in self.results

    def __bool__(self) -> bool:
        """Check if results are non-empty.

        Returns:
            bool: True if there are any results, False otherwise.
        """
        return bool(self.results)
