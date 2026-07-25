"""
core/policy.py — Policy Engine

Evaluates access control rules for each database connection.
Policies are loaded from YAML (policies/policies.yaml).

Security model:
  - Read-only by default (SELECT only)
  - Per-connection table allowlists / blocklists
  - Column-level masking (replaces sensitive values with ***)
  - Max rows per query enforcement
  - Query-level SQL validation (via query_tools.py / security.py)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from mcp_db_wrapper.core.config import load_policies, load_settings

logger = structlog.get_logger(__name__)


# ------------------------------------------------------------------ #
#  Data classes
# ------------------------------------------------------------------ #

@dataclass
class ConnectionPolicy:
    """Resolved policy for a single database connection."""

    connection_name: str
    allow_schema_introspection: bool = True
    allow_query_execution: bool = True
    allow_sample_data: bool = True
    max_rows_per_query: int = 100
    sample_data_max_rows: int = 5

    # Table / collection access
    table_allowlist: list[str] = field(default_factory=list)  # empty = allow all
    table_blocklist: list[str] = field(default_factory=list)

    # Column masks: {table_name: [col1, col2, ...]}
    column_masks: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class PolicyViolation(Exception):
    """Raised when an action violates policy."""

    message: str
    connection: str = ""
    table: str = ""
    action: str = ""

    def __str__(self) -> str:
        return (
            f"PolicyViolation[{self.connection}]: {self.message}"
            + (f" (table={self.table})" if self.table else "")
            + (f" (action={self.action})" if self.action else "")
        )


# ------------------------------------------------------------------ #
#  Policy Engine
# ------------------------------------------------------------------ #

class PolicyEngine:
    """
    Loads YAML policies and enforces them at runtime.

    Usage:
        engine = PolicyEngine()
        engine.assert_schema_access("my_postgres")
        engine.assert_table_access("my_postgres", "users")
        engine.assert_query_execution("my_postgres")
        rows = engine.apply_column_masks("my_postgres", "users", rows)
        rows = engine.enforce_row_limit("my_postgres", rows)
    """

    def __init__(self, policy_path: str | None = None) -> None:
        self._path = policy_path
        self._raw: dict = {}
        self._cache: dict[str, ConnectionPolicy] = {}
        self._reload()

    def _reload(self) -> None:
        """Reload policies from disk."""
        self._raw = load_policies(self._path)
        self._cache.clear()
        logger.info("policies_loaded", path=self._path or load_settings().policy_path)

    def _get_policy(self, connection_name: str) -> ConnectionPolicy:
        """Build and cache a ConnectionPolicy for the named connection."""
        if connection_name in self._cache:
            return self._cache[connection_name]

        defaults: dict = self._raw.get("defaults", {})
        per_conn: dict = self._raw.get("policies", {}).get(connection_name, {})

        def _get(key: str, fallback: Any) -> Any:
            # per-connection overrides defaults, which overrides hardcoded fallback
            if key in per_conn:
                return per_conn[key]
            if key in defaults:
                return defaults[key]
            return fallback

        # Table access
        tables_cfg: dict = per_conn.get("tables", per_conn.get("collections", {}))
        allowlist = tables_cfg.get("allow", [])
        blocklist = tables_cfg.get("deny", [])

        # Column masks (works for both SQL "column_masks" and Mongo "field_masks")
        col_masks: dict = per_conn.get("column_masks", per_conn.get("field_masks", {}))

        policy = ConnectionPolicy(
            connection_name=connection_name,
            allow_schema_introspection=_get("allow_schema_introspection", True),
            allow_query_execution=_get("allow_query_execution", True),
            allow_sample_data=_get("allow_sample_data", True),
            max_rows_per_query=int(_get("max_rows_per_query", 100)),
            sample_data_max_rows=int(_get("sample_data_max_rows", 5)),
            table_allowlist=[t.lower() for t in allowlist],
            table_blocklist=[t.lower() for t in blocklist],
            column_masks={
                tbl.lower(): [c.lower() for c in cols]
                for tbl, cols in col_masks.items()
            },
        )

        self._cache[connection_name] = policy
        return policy

    # -------------------------------------------------------------- #
    #  Assertion helpers (raise PolicyViolation on failure)
    # -------------------------------------------------------------- #

    def assert_schema_access(self, connection_name: str) -> None:
        """Assert that schema introspection is allowed for this connection."""
        policy = self._get_policy(connection_name)
        if not policy.allow_schema_introspection:
            raise PolicyViolation(
                message="Schema introspection is disabled by policy.",
                connection=connection_name,
                action="schema_introspection",
            )

    def assert_table_access(self, connection_name: str, table_name: str) -> None:
        """Assert that the given table is accessible under policy."""
        policy = self._get_policy(connection_name)
        t = table_name.lower()

        # Allowlist mode (if defined, ONLY these tables are accessible)
        if policy.table_allowlist and t not in policy.table_allowlist:
            raise PolicyViolation(
                message=f"Table '{table_name}' is not in the allowlist.",
                connection=connection_name,
                table=table_name,
                action="table_access",
            )

        # Blocklist mode
        if t in policy.table_blocklist:
            raise PolicyViolation(
                message=f"Table '{table_name}' is blocked by policy.",
                connection=connection_name,
                table=table_name,
                action="table_access",
            )

    def assert_query_execution(self, connection_name: str) -> None:
        """Assert that query execution is allowed for this connection."""
        policy = self._get_policy(connection_name)
        if not policy.allow_query_execution:
            raise PolicyViolation(
                message="Query execution is disabled by policy for this connection.",
                connection=connection_name,
                action="query_execution",
            )

    def assert_sample_data(self, connection_name: str) -> None:
        """Assert that sample data retrieval is allowed."""
        policy = self._get_policy(connection_name)
        if not policy.allow_sample_data:
            raise PolicyViolation(
                message="Sample data retrieval is disabled by policy.",
                connection=connection_name,
                action="sample_data",
            )

    # -------------------------------------------------------------- #
    #  Data transformation helpers
    # -------------------------------------------------------------- #

    def filter_tables(
        self, connection_name: str, tables: list[str]
    ) -> list[str]:
        """
        Filter a list of table names according to policy.

        Args:
            connection_name: The connection identifier.
            tables: Raw list of table names from the database.

        Returns:
            Filtered list of tables the LLM is allowed to see.
        """
        policy = self._get_policy(connection_name)

        def _allowed(t: str) -> bool:
            tl = t.lower()
            if policy.table_allowlist:
                return tl in policy.table_allowlist
            return tl not in policy.table_blocklist

        return [t for t in tables if _allowed(t)]

    def apply_column_masks(
        self,
        connection_name: str,
        table_name: str,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Mask sensitive column values in query results.

        Masked columns have their values replaced with '***MASKED***'.

        Args:
            connection_name: The connection identifier.
            table_name: The table being queried.
            rows: List of row dicts.

        Returns:
            Rows with sensitive columns masked.
        """
        policy = self._get_policy(connection_name)
        masked_cols = policy.column_masks.get(table_name.lower(), [])
        if not masked_cols:
            return rows

        result = []
        for row in rows:
            masked_row = {}
            for col, val in row.items():
                if col.lower() in masked_cols:
                    masked_row[col] = "***MASKED***"
                else:
                    masked_row[col] = val
            result.append(masked_row)
        return result

    def apply_schema_column_masks(
        self,
        connection_name: str,
        table_name: str,
        columns: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Mark columns as masked in schema descriptions (for LLM awareness).

        Adds a 'masked' flag to the column descriptor instead of hiding
        the column entirely — the LLM knows the column exists but that
        it cannot read the values.

        Args:
            connection_name: Connection identifier.
            table_name: Table being described.
            columns: List of column descriptor dicts with at least 'name'.

        Returns:
            Column descriptors with 'masked: true/false' flag added.
        """
        policy = self._get_policy(connection_name)
        masked_cols = policy.column_masks.get(table_name.lower(), [])

        result = []
        for col in columns:
            col_copy = dict(col)
            col_copy["masked"] = col_copy.get("name", "").lower() in masked_cols
            result.append(col_copy)
        return result

    def enforce_row_limit(
        self,
        connection_name: str,
        rows: list[Any],
        is_sample: bool = False,
    ) -> list[Any]:
        """
        Truncate rows to the policy maximum.

        Args:
            connection_name: Connection identifier.
            rows: Raw row list.
            is_sample: If True, use sample_data_max_rows limit instead.

        Returns:
            Truncated row list.
        """
        policy = self._get_policy(connection_name)
        limit = policy.sample_data_max_rows if is_sample else policy.max_rows_per_query
        return rows[:limit]

    def get_row_limit(self, connection_name: str, is_sample: bool = False) -> int:
        """Get the effective row limit for a connection."""
        policy = self._get_policy(connection_name)
        return policy.sample_data_max_rows if is_sample else policy.max_rows_per_query

    def get_policy_summary(self, connection_name: str) -> dict[str, Any]:
        """Return a human-readable policy summary for a connection."""
        policy = self._get_policy(connection_name)
        return {
            "connection": connection_name,
            "allow_schema_introspection": policy.allow_schema_introspection,
            "allow_query_execution": policy.allow_query_execution,
            "allow_sample_data": policy.allow_sample_data,
            "max_rows_per_query": policy.max_rows_per_query,
            "table_allowlist": policy.table_allowlist or "all",
            "table_blocklist": policy.table_blocklist or "none",
            "masked_tables": list(policy.column_masks.keys()),
        }
