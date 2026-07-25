"""
core/schema.py — Schema Introspector

Provides high-level schema introspection operations that combine
connector data with policy filtering and column masking.

This is the layer that the MCP tools call — not the connector directly.
"""
from __future__ import annotations

from typing import Any

import structlog

from mcp_db_wrapper.connectors.base import RelationshipInfo, TableInfo
from mcp_db_wrapper.connectors.mongodb import MongoDBConnector
from mcp_db_wrapper.core.policy import PolicyEngine, PolicyViolation
from mcp_db_wrapper.core.registry import ConnectorRegistry

logger = structlog.get_logger(__name__)


class SchemaIntrospector:
    """
    High-level schema introspection that respects policies.

    All methods enforce policy rules before returning data to the LLM.
    """

    def __init__(self, registry: ConnectorRegistry, policy: PolicyEngine) -> None:
        self._registry = registry
        self._policy = policy

    # -------------------------------------------------------------- #
    #  Schema operations
    # -------------------------------------------------------------- #

    async def list_tables(self, connection_name: str) -> dict[str, Any]:
        """
        List tables/collections for a connection (policy-filtered).

        Args:
            connection_name: The connection to introspect.

        Returns:
            Dict with 'tables' list and metadata.
        """
        self._policy.assert_schema_access(connection_name)
        connector = await self._registry.get(connection_name)
        all_tables = await connector.list_tables()
        visible = self._policy.filter_tables(connection_name, all_tables)
        hidden_count = len(all_tables) - len(visible)

        return {
            "connection": connection_name,
            "db_type": connector.DB_TYPE,
            "tables": visible,
            "total_visible": len(visible),
            "hidden_by_policy": hidden_count,
        }

    async def describe_table(
        self, connection_name: str, table_name: str
    ) -> dict[str, Any]:
        """
        Describe a table with policy-applied column masking.

        Args:
            connection_name: The connection to use.
            table_name: The table to describe.

        Returns:
            Dict with table schema info.
        """
        self._policy.assert_schema_access(connection_name)
        self._policy.assert_table_access(connection_name, table_name)

        connector = await self._registry.get(connection_name)
        table_info: TableInfo = await connector.describe_table(table_name)

        # Apply column mask flags to schema description
        raw_cols = [c.to_dict() for c in table_info.columns]
        masked_cols = self._policy.apply_schema_column_masks(
            connection_name, table_name, raw_cols
        )

        return {
            "connection": connection_name,
            "table": table_name,
            "schema": table_info.schema,
            "table_type": table_info.table_type,
            "row_count": table_info.row_count,
            "comment": table_info.comment,
            "columns": masked_cols,
            "column_count": len(masked_cols),
        }

    async def get_schema_map(self, connection_name: str) -> dict[str, Any]:
        """
        Return the full schema map for a connection (policy-filtered).

        Args:
            connection_name: The connection to introspect.

        Returns:
            Dict mapping table names to their schema info.
        """
        self._policy.assert_schema_access(connection_name)
        connector = await self._registry.get(connection_name)
        full_map: dict[str, TableInfo] = await connector.get_schema_map()

        # Filter tables by policy
        visible_names = self._policy.filter_tables(connection_name, list(full_map.keys()))
        visible_map: dict[str, Any] = {}

        for table_name in visible_names:
            try:
                table_info = full_map[table_name]
                raw_cols = [c.to_dict() for c in table_info.columns]
                masked_cols = self._policy.apply_schema_column_masks(
                    connection_name, table_name, raw_cols
                )
                visible_map[table_name] = {
                    "schema": table_info.schema,
                    "table_type": table_info.table_type,
                    "row_count": table_info.row_count,
                    "columns": masked_cols,
                }
            except Exception as e:
                logger.warning(
                    "schema_map_table_error",
                    table=table_name,
                    error=str(e),
                )
                visible_map[table_name] = {"error": str(e)}

        return {
            "connection": connection_name,
            "db_type": connector.DB_TYPE,
            "schema_map": visible_map,
            "table_count": len(visible_map),
        }

    async def get_relationships(self, connection_name: str) -> dict[str, Any]:
        """
        Return FK / reference relationships (filtered to visible tables).

        Args:
            connection_name: The connection to introspect.

        Returns:
            Dict with relationships list.
        """
        self._policy.assert_schema_access(connection_name)
        connector = await self._registry.get(connection_name)
        all_rels: list[RelationshipInfo] = await connector.get_relationships()

        # Only include relationships where BOTH tables are policy-visible
        all_tables = await connector.list_tables()
        visible = set(self._policy.filter_tables(connection_name, all_tables))

        filtered = [
            r.to_dict()
            for r in all_rels
            if r.from_table.lower() in {t.lower() for t in visible}
            and r.to_table.lower() in {t.lower() for t in visible}
        ]

        return {
            "connection": connection_name,
            "relationships": filtered,
            "total": len(filtered),
        }

    async def execute_query(
        self,
        connection_name: str,
        sql: str,
        table_hint: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute a validated SELECT query with policy and security checks.

        Args:
            connection_name: The connection to query.
            sql: The SQL string to execute (SELECT only).
            table_hint: Optional table name for column masking (inferred if not provided).

        Returns:
            Dict with query results.
        """
        from mcp_db_wrapper.core.security import get_query_validator

        # Step 1: Policy check
        self._policy.assert_query_execution(connection_name)

        # Step 2: Security / SQL validation
        connector = await self._registry.get(connection_name)

        if isinstance(connector, MongoDBConnector):
            raise PolicyViolation(
                message="Use execute_mongo_query for MongoDB connections.",
                connection=connection_name,
                action="execute_query",
            )

        validator = get_query_validator()
        clean_sql = validator.validate(sql, dialect=connector.get_dialect())

        # Step 3: Execute with row limit
        row_limit = self._policy.get_row_limit(connection_name)
        rows = await connector.execute_query(clean_sql, limit=row_limit)
        rows = self._policy.enforce_row_limit(connection_name, rows)

        # Step 4: Apply column masks (if table_hint is provided)
        if table_hint:
            rows = self._policy.apply_column_masks(connection_name, table_hint, rows)

        return {
            "connection": connection_name,
            "sql": clean_sql,
            "row_count": len(rows),
            "row_limit": row_limit,
            "rows": rows,
        }

    async def get_sample_data(
        self, connection_name: str, table_name: str
    ) -> dict[str, Any]:
        """
        Return sample rows from a table with policy applied.

        Args:
            connection_name: The connection to use.
            table_name: The table to sample.

        Returns:
            Dict with sample rows.
        """
        self._policy.assert_sample_data(connection_name)
        self._policy.assert_table_access(connection_name, table_name)

        connector = await self._registry.get(connection_name)
        limit = self._policy.get_row_limit(connection_name, is_sample=True)
        rows = await connector.get_sample_data(table_name, limit=limit)
        rows = self._policy.apply_column_masks(connection_name, table_name, rows)

        return {
            "connection": connection_name,
            "table": table_name,
            "sample_rows": rows,
            "count": len(rows),
            "limit": limit,
        }

    async def get_db_stats(self, connection_name: str) -> dict[str, Any]:
        """Return database statistics."""
        self._policy.assert_schema_access(connection_name)
        connector = await self._registry.get(connection_name)
        return await connector.get_db_stats()
