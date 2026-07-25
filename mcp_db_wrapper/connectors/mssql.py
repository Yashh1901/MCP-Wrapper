"""
connectors/mssql.py — Microsoft SQL Server Connector (async via aioodbc)
"""
from __future__ import annotations

from typing import Any

import aioodbc
import structlog

from mcp_db_wrapper.connectors.base import (
    BaseConnector,
    ColumnInfo,
    RelationshipInfo,
    TableInfo,
)
from mcp_db_wrapper.core.config import ConnectionConfig

logger = structlog.get_logger(__name__)


class MSSQLConnector(BaseConnector):
    DB_TYPE = "mssql"

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._pool: aioodbc.Pool | None = None

    def _build_dsn(self) -> str:
        raw = self.config.raw
        driver = raw.get("driver", "ODBC Driver 17 for SQL Server")
        host = raw.get("host", "localhost")
        port = raw.get("port", 1433)
        db = raw.get("database", "master")
        user = raw.get("user", "")
        password = raw.get("password", "")
        return (
            f"DRIVER={{{driver}}};SERVER={host},{port};"
            f"DATABASE={db};UID={user};PWD={password};TrustServerCertificate=yes"
        )

    async def connect(self) -> None:
        dsn = self._build_dsn()
        logger.info("mssql_connecting", connection=self.name)
        self._pool = await aioodbc.create_pool(dsn=dsn, minsize=1, maxsize=5)
        self._connected = True
        logger.info("mssql_connected", connection=self.name)

    async def disconnect(self) -> None:
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._connected = False

    async def list_tables(self) -> list[str]:
        assert self._pool, "Not connected"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT TABLE_NAME
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW')
                      AND TABLE_SCHEMA = 'dbo'
                    ORDER BY TABLE_NAME
                    """
                )
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def describe_table(self, table_name: str) -> TableInfo:
        assert self._pool, "Not connected"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT
                        c.COLUMN_NAME, c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH,
                        c.IS_NULLABLE, c.COLUMN_DEFAULT,
                        CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END AS is_pk,
                        CASE WHEN uq.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END AS is_uq
                    FROM INFORMATION_SCHEMA.COLUMNS c
                    LEFT JOIN (
                        SELECT kcu.COLUMN_NAME
                        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                            ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                          AND tc.TABLE_NAME = ?
                          AND tc.TABLE_SCHEMA = 'dbo'
                    ) pk ON c.COLUMN_NAME = pk.COLUMN_NAME
                    LEFT JOIN (
                        SELECT kcu.COLUMN_NAME
                        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                            ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                        WHERE tc.CONSTRAINT_TYPE = 'UNIQUE'
                          AND tc.TABLE_NAME = ?
                          AND tc.TABLE_SCHEMA = 'dbo'
                    ) uq ON c.COLUMN_NAME = uq.COLUMN_NAME
                    WHERE c.TABLE_NAME = ? AND c.TABLE_SCHEMA = 'dbo'
                    ORDER BY c.ORDINAL_POSITION
                    """,
                    table_name, table_name, table_name,
                )
                col_rows = await cur.fetchall()

                await cur.execute(
                    """
                    SELECT
                        kcu.COLUMN_NAME, ccu.TABLE_NAME AS ref_table, ccu.COLUMN_NAME AS ref_col
                    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                        ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                    JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
                        ON tc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
                    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ccu
                        ON rc.UNIQUE_CONSTRAINT_NAME = ccu.CONSTRAINT_NAME
                    WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY'
                      AND tc.TABLE_NAME = ? AND tc.TABLE_SCHEMA = 'dbo'
                    """,
                    table_name,
                )
                fk_rows = await cur.fetchall()

        fk_map = {r[0]: f"{r[1]}.{r[2]}" for r in fk_rows}
        columns = [
            ColumnInfo(
                name=r[0], data_type=r[1], max_length=r[2],
                nullable=(r[3] == "YES"), default=r[4],
                is_primary_key=bool(r[5]), is_unique=bool(r[6]),
                is_foreign_key=r[0] in fk_map, foreign_key_ref=fk_map.get(r[0]),
            )
            for r in col_rows
        ]
        return TableInfo(name=table_name, schema="dbo", columns=columns)

    async def get_schema_map(self) -> dict[str, TableInfo]:
        tables = await self.list_tables()
        return {t: await self.describe_table(t) for t in tables}

    async def get_relationships(self) -> list[RelationshipInfo]:
        assert self._pool, "Not connected"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT
                        kcu.TABLE_NAME AS from_table, kcu.COLUMN_NAME AS from_col,
                        ccu.TABLE_NAME AS to_table, ccu.COLUMN_NAME AS to_col,
                        tc.CONSTRAINT_NAME
                    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                        ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                    JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
                        ON tc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
                    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ccu
                        ON rc.UNIQUE_CONSTRAINT_NAME = ccu.CONSTRAINT_NAME
                    WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY'
                      AND tc.TABLE_SCHEMA = 'dbo'
                    """
                )
                rows = await cur.fetchall()
        return [
            RelationshipInfo(from_table=r[0], from_column=r[1], to_table=r[2], to_column=r[3], constraint_name=r[4])
            for r in rows
        ]

    async def execute_query(
        self, sql: str, params: list[Any] | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        assert self._pool, "Not connected"
        safe_sql = sql if "TOP" in sql.upper() or "FETCH" in sql.upper() else f"SELECT TOP {limit} * FROM ({sql}) _sub"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(safe_sql, *(params or []))
                cols = [c[0] for c in cur.description]
                rows = await cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    async def get_sample_data(self, table_name: str, limit: int = 5) -> list[dict[str, Any]]:
        return await self.execute_query(f"SELECT TOP {limit} * FROM [{table_name}]")
