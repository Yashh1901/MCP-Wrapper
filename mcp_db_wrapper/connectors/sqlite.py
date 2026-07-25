"""
connectors/sqlite.py — SQLite Connector (async via aiosqlite)
"""
from __future__ import annotations

from typing import Any

import aiosqlite
import structlog

from mcp_db_wrapper.connectors.base import (
    BaseConnector,
    ColumnInfo,
    RelationshipInfo,
    TableInfo,
)
from mcp_db_wrapper.core.config import ConnectionConfig

logger = structlog.get_logger(__name__)


class SQLiteConnector(BaseConnector):
    DB_TYPE = "sqlite"

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._conn: aiosqlite.Connection | None = None
        self._path: str = config.get("path", ":memory:")

    async def connect(self) -> None:
        logger.info("sqlite_connecting", path=self._path)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        self._connected = True

    async def disconnect(self) -> None:
        if self._conn:
            await self._conn.close()
            self._connected = False

    async def list_tables(self) -> list[str]:
        assert self._conn, "Not connected"
        async with self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        return [r["name"] for r in rows]

    async def describe_table(self, table_name: str) -> TableInfo:
        assert self._conn, "Not connected"
        async with self._conn.execute(f"PRAGMA table_info('{table_name}')") as cur:
            col_rows = await cur.fetchall()
        async with self._conn.execute(f"PRAGMA foreign_key_list('{table_name}')") as cur:
            fk_rows = await cur.fetchall()
        async with self._conn.execute(f"SELECT COUNT(*) AS cnt FROM \"{table_name}\"") as cur:
            count_row = await cur.fetchone()

        fk_map = {r["from"]: f"{r['table']}.{r['to']}" for r in fk_rows}

        columns = [
            ColumnInfo(
                name=r["name"],
                data_type=r["type"],
                nullable=(not r["notnull"]),
                default=r["dflt_value"],
                is_primary_key=bool(r["pk"]),
                is_foreign_key=r["name"] in fk_map,
                foreign_key_ref=fk_map.get(r["name"]),
            )
            for r in col_rows
        ]

        return TableInfo(
            name=table_name,
            columns=columns,
            row_count=count_row["cnt"] if count_row else None,
        )

    async def get_schema_map(self) -> dict[str, TableInfo]:
        tables = await self.list_tables()
        return {t: await self.describe_table(t) for t in tables}

    async def get_relationships(self) -> list[RelationshipInfo]:
        tables = await self.list_tables()
        result = []
        for table in tables:
            assert self._conn, "Not connected"
            async with self._conn.execute(f"PRAGMA foreign_key_list('{table}')") as cur:
                rows = await cur.fetchall()
            for r in rows:
                result.append(
                    RelationshipInfo(
                        from_table=table,
                        from_column=r["from"],
                        to_table=r["table"],
                        to_column=r["to"],
                    )
                )
        return result

    async def execute_query(
        self, sql: str, params: list[Any] | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        assert self._conn, "Not connected"
        safe_sql = sql if "LIMIT" in sql.upper() else f"{sql} LIMIT {limit}"
        async with self._conn.execute(safe_sql, params or []) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_sample_data(self, table_name: str, limit: int = 5) -> list[dict[str, Any]]:
        return await self.execute_query(f'SELECT * FROM "{table_name}" LIMIT {limit}')

    async def get_db_stats(self) -> dict[str, Any]:
        assert self._conn, "Not connected"
        tables = await self.list_tables()
        counts = {}
        for t in tables:
            async with self._conn.execute(f'SELECT COUNT(*) AS c FROM "{t}"') as cur:
                row = await cur.fetchone()
            counts[t] = row["c"] if row else 0
        return {
            "db_type": self.DB_TYPE,
            "connection": self.name,
            "path": self._path,
            "table_count": len(tables),
            "table_row_counts": counts,
        }
