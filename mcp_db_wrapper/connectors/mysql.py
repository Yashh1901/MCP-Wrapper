"""
connectors/mysql.py — MySQL Connector

Uses aiomysql for async connections.
Introspects schema via INFORMATION_SCHEMA queries.
"""
from __future__ import annotations

from typing import Any

import aiomysql
import structlog

from mcp_db_wrapper.connectors.base import (
    BaseConnector,
    ColumnInfo,
    RelationshipInfo,
    TableInfo,
)
from mcp_db_wrapper.core.config import ConnectionConfig

logger = structlog.get_logger(__name__)


class MySQLConnector(BaseConnector):
    """MySQL / MariaDB connector using aiomysql."""

    DB_TYPE = "mysql"

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        raw = self.config.raw
        logger.info("mysql_connecting", connection=self.name)
        self._pool = await aiomysql.create_pool(
            host=raw.get("host", "localhost"),
            port=int(raw.get("port", 3306)),
            db=raw.get("database", ""),
            user=raw.get("user", "root"),
            password=raw.get("password", ""),
            charset="utf8mb4",
            autocommit=True,
        )
        self._connected = True
        logger.info("mysql_connected", connection=self.name)

    async def disconnect(self) -> None:
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._connected = False

    async def list_tables(self) -> list[str]:
        assert self._pool, "Not connected"
        db = self.config.get("database", "")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT TABLE_NAME
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
                    ORDER BY TABLE_NAME
                    """,
                    (db,),
                )
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def describe_table(self, table_name: str) -> TableInfo:
        assert self._pool, "Not connected"
        db = self.config.get("database", "")
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                # Columns
                await cur.execute(
                    """
                    SELECT
                        COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
                        IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY, EXTRA
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    ORDER BY ORDINAL_POSITION
                    """,
                    (db, table_name),
                )
                col_rows = await cur.fetchall()

                # Foreign keys
                await cur.execute(
                    """
                    SELECT
                        COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME,
                        CONSTRAINT_NAME
                    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                      AND REFERENCED_TABLE_NAME IS NOT NULL
                    """,
                    (db, table_name),
                )
                fk_rows = await cur.fetchall()

                # Row count estimate
                await cur.execute(
                    "SELECT TABLE_ROWS FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                    (db, table_name),
                )
                cnt = await cur.fetchone()

        fk_map = {
            r["COLUMN_NAME"]: f"{r['REFERENCED_TABLE_NAME']}.{r['REFERENCED_COLUMN_NAME']}"
            for r in fk_rows
        }

        columns = [
            ColumnInfo(
                name=r["COLUMN_NAME"],
                data_type=r["DATA_TYPE"],
                nullable=(r["IS_NULLABLE"] == "YES"),
                default=r["COLUMN_DEFAULT"],
                is_primary_key=(r["COLUMN_KEY"] == "PRI"),
                is_unique=(r["COLUMN_KEY"] in ("PRI", "UNI")),
                is_foreign_key=r["COLUMN_NAME"] in fk_map,
                foreign_key_ref=fk_map.get(r["COLUMN_NAME"]),
                max_length=r["CHARACTER_MAXIMUM_LENGTH"],
            )
            for r in col_rows
        ]

        return TableInfo(
            name=table_name,
            columns=columns,
            row_count=cnt["TABLE_ROWS"] if cnt else None,
        )

    async def get_schema_map(self) -> dict[str, TableInfo]:
        tables = await self.list_tables()
        return {t: await self.describe_table(t) for t in tables}

    async def get_relationships(self) -> list[RelationshipInfo]:
        assert self._pool, "Not connected"
        db = self.config.get("database", "")
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT
                        TABLE_NAME AS from_table,
                        COLUMN_NAME AS from_column,
                        REFERENCED_TABLE_NAME AS to_table,
                        REFERENCED_COLUMN_NAME AS to_column,
                        CONSTRAINT_NAME AS constraint_name
                    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = %s
                      AND REFERENCED_TABLE_NAME IS NOT NULL
                    """,
                    (db,),
                )
                rows = await cur.fetchall()

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

    async def execute_query(
        self,
        sql: str,
        params: list[Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        assert self._pool, "Not connected"
        safe_sql = self._inject_limit(sql, limit)
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(safe_sql, params or ())
                rows = await cur.fetchall()
        return list(rows)

    async def get_sample_data(self, table_name: str, limit: int = 5) -> list[dict[str, Any]]:
        return await self.execute_query(f"SELECT * FROM `{table_name}` LIMIT {limit}")

    async def get_db_stats(self) -> dict[str, Any]:
        assert self._pool, "Not connected"
        db = self.config.get("database", "")
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SELECT VERSION() AS version")
                ver = await cur.fetchone()
                await cur.execute(
                    """
                    SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = %s
                    ORDER BY DATA_LENGTH DESC
                    """,
                    (db,),
                )
                tables = await cur.fetchall()
        return {
            "db_type": self.DB_TYPE,
            "connection": self.name,
            "database": db,
            "version": ver["version"] if ver else "unknown",
            "tables": [
                {
                    "table": t["TABLE_NAME"],
                    "rows": t["TABLE_ROWS"],
                    "data_size_bytes": t["DATA_LENGTH"],
                    "index_size_bytes": t["INDEX_LENGTH"],
                }
                for t in tables
            ],
        }

    @staticmethod
    def _inject_limit(sql: str, limit: int) -> str:
        if "LIMIT" not in sql.upper():
            return f"{sql} LIMIT {limit}"
        return sql
