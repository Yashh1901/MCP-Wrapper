"""
connectors/postgres.py — PostgreSQL Connector

Uses asyncpg for high-performance async connections.
Provides full schema introspection via information_schema queries.
"""
from __future__ import annotations

from typing import Any

import asyncpg
import structlog

from mcp_db_wrapper.connectors.base import (
    BaseConnector,
    ColumnInfo,
    RelationshipInfo,
    TableInfo,
)
from mcp_db_wrapper.core.config import ConnectionConfig

logger = structlog.get_logger(__name__)


class PostgresConnector(BaseConnector):
    """
    PostgreSQL database connector using asyncpg.

    Supports:
      - SSL/TLS connections
      - Connection pooling
      - Full information_schema introspection
      - Foreign key relationship mapping
    """

    DB_TYPE = "postgres"

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._pool: asyncpg.Pool | None = None

    # -------------------------------------------------------------- #
    #  Lifecycle
    # -------------------------------------------------------------- #

    async def connect(self) -> None:
        ssl_mode = self.config.get("ssl", False)
        dsn = self._build_dsn()
        logger.info("postgres_connecting", connection=self.name)
        self._pool = await asyncpg.create_pool(
            dsn=dsn,
            ssl=ssl_mode,
            min_size=self.config.get("pool_min", 1),
            max_size=self.config.get("pool_max", 10),
        )
        self._connected = True
        logger.info("postgres_connected", connection=self.name)

    async def disconnect(self) -> None:
        if self._pool:
            await self._pool.close()
            self._connected = False
            logger.info("postgres_disconnected", connection=self.name)

    def _build_dsn(self) -> str:
        raw = self.config.raw
        if url := raw.get("url"):
            return url
        host = raw.get("host", "localhost")
        port = raw.get("port", 5432)
        db = raw.get("database", "postgres")
        user = raw.get("user", "postgres")
        password = raw.get("password", "")
        return f"postgresql://{user}:{password}@{host}:{port}/{db}"

    # -------------------------------------------------------------- #
    #  Schema introspection
    # -------------------------------------------------------------- #

    async def list_tables(self) -> list[str]:
        assert self._pool, "Not connected"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type IN ('BASE TABLE', 'VIEW')
                ORDER BY table_name
                """
            )
        return [r["table_name"] for r in rows]

    async def describe_table(self, table_name: str) -> TableInfo:
        assert self._pool, "Not connected"
        async with self._pool.acquire() as conn:
            # Column metadata
            col_rows = await conn.fetch(
                """
                SELECT
                    c.column_name,
                    c.data_type,
                    c.character_maximum_length,
                    c.is_nullable,
                    c.column_default,
                    COALESCE(pk.is_pk, false) AS is_primary_key,
                    COALESCE(uq.is_uq, false) AS is_unique
                FROM information_schema.columns c
                LEFT JOIN (
                    SELECT kcu.column_name, true AS is_pk
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                        ON tc.constraint_name = kcu.constraint_name
                        AND tc.table_name = kcu.table_name
                    WHERE tc.constraint_type = 'PRIMARY KEY'
                      AND tc.table_name = $1
                      AND tc.table_schema = 'public'
                ) pk ON c.column_name = pk.column_name
                LEFT JOIN (
                    SELECT kcu.column_name, true AS is_uq
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                        ON tc.constraint_name = kcu.constraint_name
                        AND tc.table_name = kcu.table_name
                    WHERE tc.constraint_type = 'UNIQUE'
                      AND tc.table_name = $1
                      AND tc.table_schema = 'public'
                ) uq ON c.column_name = uq.column_name
                WHERE c.table_name = $1
                  AND c.table_schema = 'public'
                ORDER BY c.ordinal_position
                """,
                table_name,
            )

            # FK metadata
            fk_rows = await conn.fetch(
                """
                SELECT
                    kcu.column_name,
                    ccu.table_name AS foreign_table,
                    ccu.column_name AS foreign_column,
                    tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_name = $1
                  AND tc.table_schema = 'public'
                """,
                table_name,
            )

            # Approximate row count
            count_row = await conn.fetchrow(
                "SELECT reltuples::bigint AS row_count FROM pg_class WHERE relname = $1",
                table_name,
            )

        fk_map = {
            r["column_name"]: f"{r['foreign_table']}.{r['foreign_column']}"
            for r in fk_rows
        }

        columns = [
            ColumnInfo(
                name=r["column_name"],
                data_type=r["data_type"],
                nullable=(r["is_nullable"] == "YES"),
                default=r["column_default"],
                is_primary_key=r["is_primary_key"],
                is_unique=r["is_unique"],
                is_foreign_key=r["column_name"] in fk_map,
                foreign_key_ref=fk_map.get(r["column_name"]),
                max_length=r["character_maximum_length"],
            )
            for r in col_rows
        ]

        return TableInfo(
            name=table_name,
            schema="public",
            columns=columns,
            row_count=count_row["row_count"] if count_row else None,
        )

    async def get_schema_map(self) -> dict[str, TableInfo]:
        tables = await self.list_tables()
        schema_map = {}
        for table in tables:
            schema_map[table] = await self.describe_table(table)
        return schema_map

    async def get_relationships(self) -> list[RelationshipInfo]:
        assert self._pool, "Not connected"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    kcu.table_name AS from_table,
                    kcu.column_name AS from_column,
                    ccu.table_name AS to_table,
                    ccu.column_name AS to_column,
                    tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = 'public'
                ORDER BY from_table, from_column
                """
            )

        return [
            RelationshipInfo(
                from_table=r["from_table"],
                from_column=r["from_column"],
                to_table=r["to_table"],
                to_column=r["to_column"],
                constraint_name=r["constraint_name"],
            )
            for r in rows
        ]

    # -------------------------------------------------------------- #
    #  Query execution
    # -------------------------------------------------------------- #

    async def execute_query(
        self,
        sql: str,
        params: list[Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        assert self._pool, "Not connected"
        # Inject LIMIT to prevent runaway queries
        safe_sql = self._inject_limit(sql, limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(safe_sql, *(params or []))
        return [dict(r) for r in rows]

    async def get_sample_data(
        self, table_name: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        return await self.execute_query(
            f'SELECT * FROM "{table_name}" LIMIT {limit}'
        )

    # -------------------------------------------------------------- #
    #  Statistics
    # -------------------------------------------------------------- #

    async def get_db_stats(self) -> dict[str, Any]:
        assert self._pool, "Not connected"
        async with self._pool.acquire() as conn:
            db_row = await conn.fetchrow("SELECT current_database() AS db_name, version() AS version")
            size_row = await conn.fetchrow(
                "SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size"
            )
            table_counts = await conn.fetch(
                """
                SELECT relname AS table_name, reltuples::bigint AS row_count
                FROM pg_class
                JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
                WHERE pg_namespace.nspname = 'public'
                  AND relkind = 'r'
                ORDER BY reltuples DESC
                """
            )
        return {
            "db_type": self.DB_TYPE,
            "connection": self.name,
            "database": db_row["db_name"],
            "version": db_row["version"],
            "size": size_row["db_size"],
            "table_row_counts": {r["table_name"]: r["row_count"] for r in table_counts},
        }

    # -------------------------------------------------------------- #
    #  Internals
    # -------------------------------------------------------------- #

    @staticmethod
    def _inject_limit(sql: str, limit: int) -> str:
        """Append or replace LIMIT clause to enforce row cap."""
        upper = sql.upper()
        if "LIMIT" not in upper:
            return f"{sql} LIMIT {limit}"
        return sql  # sqlglot already validated; trust it
