"""
connectors/base.py — Abstract Base Connector

All database connectors must implement this interface.
Connectors handle:
  - Connection lifecycle (connect / disconnect)
  - Schema introspection (list tables, describe table, full schema map)
  - Query execution (SELECT only — security enforced at a higher layer)
  - Sample data retrieval
  - DB statistics
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


# ------------------------------------------------------------------ #
#  Data models (simple dataclasses — no ORM overhead)
# ------------------------------------------------------------------ #

class ColumnInfo:
    """Descriptor for a single table column."""

    def __init__(
        self,
        name: str,
        data_type: str,
        nullable: bool = True,
        default: Any = None,
        is_primary_key: bool = False,
        is_foreign_key: bool = False,
        foreign_key_ref: str | None = None,  # "table.column"
        is_unique: bool = False,
        is_indexed: bool = False,
        max_length: int | None = None,
        masked: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.data_type = data_type
        self.nullable = nullable
        self.default = default
        self.is_primary_key = is_primary_key
        self.is_foreign_key = is_foreign_key
        self.foreign_key_ref = foreign_key_ref
        self.is_unique = is_unique
        self.is_indexed = is_indexed
        self.max_length = max_length
        self.masked = masked
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "data_type": self.data_type,
            "nullable": self.nullable,
            "default": self.default,
            "is_primary_key": self.is_primary_key,
            "is_foreign_key": self.is_foreign_key,
            "foreign_key_ref": self.foreign_key_ref,
            "is_unique": self.is_unique,
            "is_indexed": self.is_indexed,
            "max_length": self.max_length,
            "masked": self.masked,
            **self.extra,
        }


class TableInfo:
    """Descriptor for a single table / collection."""

    def __init__(
        self,
        name: str,
        schema: str | None = None,
        columns: list[ColumnInfo] | None = None,
        row_count: int | None = None,
        table_type: str = "TABLE",  # TABLE | VIEW | COLLECTION
        comment: str | None = None,
    ) -> None:
        self.name = name
        self.schema = schema
        self.columns = columns or []
        self.row_count = row_count
        self.table_type = table_type
        self.comment = comment

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schema": self.schema,
            "columns": [c.to_dict() for c in self.columns],
            "row_count": self.row_count,
            "table_type": self.table_type,
            "comment": self.comment,
        }


class RelationshipInfo:
    """Describes a foreign key / reference relationship between tables."""

    def __init__(
        self,
        from_table: str,
        from_column: str,
        to_table: str,
        to_column: str,
        constraint_name: str | None = None,
    ) -> None:
        self.from_table = from_table
        self.from_column = from_column
        self.to_table = to_table
        self.to_column = to_column
        self.constraint_name = constraint_name

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_table": self.from_table,
            "from_column": self.from_column,
            "to_table": self.to_table,
            "to_column": self.to_column,
            "constraint_name": self.constraint_name,
        }


# ------------------------------------------------------------------ #
#  Abstract Base Connector
# ------------------------------------------------------------------ #

class BaseConnector(ABC):
    """
    Abstract connector that all database drivers must implement.

    Lifecycle:
        connector = PostgresConnector(config)
        await connector.connect()
        # ... use connector ...
        await connector.disconnect()

    Alternatively use as an async context manager:
        async with PostgresConnector(config) as conn:
            tables = await conn.list_tables()
    """

    DB_TYPE: str = "unknown"  # e.g. "postgres", "mysql", "mongodb"

    def __init__(self, config: Any) -> None:
        """
        Args:
            config: ConnectionConfig instance.
        """
        self.config = config
        self.name: str = config.name
        self._connected: bool = False

    # -------------------------------------------------------------- #
    #  Lifecycle
    # -------------------------------------------------------------- #

    @abstractmethod
    async def connect(self) -> None:
        """Establish the database connection."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the database connection cleanly."""

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def __aenter__(self) -> "BaseConnector":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()

    # -------------------------------------------------------------- #
    #  Schema introspection
    # -------------------------------------------------------------- #

    @abstractmethod
    async def list_tables(self) -> list[str]:
        """
        Return a list of table / collection names in the database.

        Returns:
            List of table name strings.
        """

    @abstractmethod
    async def describe_table(self, table_name: str) -> TableInfo:
        """
        Return detailed schema information for a single table.

        Args:
            table_name: Name of the table / collection.

        Returns:
            TableInfo object.
        """

    @abstractmethod
    async def get_schema_map(self) -> dict[str, TableInfo]:
        """
        Return the full database schema as a dict of TableInfo objects.

        Returns:
            Dict mapping table_name -> TableInfo.
        """

    @abstractmethod
    async def get_relationships(self) -> list[RelationshipInfo]:
        """
        Return foreign key / reference relationships between tables.

        Returns:
            List of RelationshipInfo objects.
        """

    # -------------------------------------------------------------- #
    #  Query execution
    # -------------------------------------------------------------- #

    @abstractmethod
    async def execute_query(
        self,
        sql: str,
        params: list[Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Execute a validated SELECT query and return results as a list of dicts.

        IMPORTANT: This method should ONLY be called after the query has been
        validated by QueryValidator. Do not call this with unvalidated SQL.

        Args:
            sql: Validated SELECT SQL string.
            params: Optional parameterised query values.
            limit: Hard cap on returned rows.

        Returns:
            List of row dicts.
        """

    # -------------------------------------------------------------- #
    #  Sample data
    # -------------------------------------------------------------- #

    async def get_sample_data(
        self, table_name: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """
        Return a sample of rows from a table.

        Default implementation builds a simple SELECT. Subclasses
        can override for NoSQL or performance reasons.

        Args:
            table_name: The table to sample.
            limit: Max rows to return.

        Returns:
            List of row dicts.
        """
        # Default: execute a simple SELECT LIMIT query
        # Subclasses should override for NoSQL databases
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement get_sample_data()"
        )

    # -------------------------------------------------------------- #
    #  Statistics
    # -------------------------------------------------------------- #

    async def get_db_stats(self) -> dict[str, Any]:
        """
        Return high-level database statistics.

        Returns:
            Dict with stats (row counts, index info, sizes, etc.)
        """
        return {"db_type": self.DB_TYPE, "connection": self.name}

    # -------------------------------------------------------------- #
    #  Utilities
    # -------------------------------------------------------------- #

    def get_dialect(self) -> str | None:
        """Return the sqlglot dialect string for this connector."""
        _DIALECT_MAP = {
            "postgres": "postgres",
            "mysql": "mysql",
            "sqlite": "sqlite",
            "mssql": "tsql",
            "mongodb": None,
            "redis": None,
            "supabase": "postgres",
        }
        return _DIALECT_MAP.get(self.DB_TYPE)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} connected={self._connected}>"
