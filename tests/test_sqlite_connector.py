"""
tests/test_sqlite_connector.py — Integration test using real SQLite (no external DB needed)
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from mcp_db_wrapper.connectors.sqlite import SQLiteConnector
from mcp_db_wrapper.core.config import ConnectionConfig


# ------------------------------------------------------------------ #
#  Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Create a temp SQLite DB with test schema."""
    path = str(tmp_path / "test.db")

    async def _setup() -> None:
        async with aiosqlite.connect(path) as conn:
            await conn.execute("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    total REAL NOT NULL,
                    status TEXT DEFAULT 'pending',
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """)
            await conn.executemany(
                "INSERT INTO users (name, email) VALUES (?, ?)",
                [
                    ("Alice", "alice@example.com"),
                    ("Bob", "bob@example.com"),
                    ("Charlie", "charlie@example.com"),
                ],
            )
            await conn.executemany(
                "INSERT INTO orders (user_id, total, status) VALUES (?, ?, ?)",
                [
                    (1, 99.99, "completed"),
                    (1, 149.50, "pending"),
                    (2, 75.00, "completed"),
                ],
            )
            await conn.commit()

    asyncio.run(_setup())
    return path


@pytest_asyncio.fixture
async def connector(db_path: str) -> SQLiteConnector:
    config = ConnectionConfig("test_sqlite", {"type": "sqlite", "path": db_path})
    conn = SQLiteConnector(config)
    await conn.connect()
    yield conn
    await conn.disconnect()


# ------------------------------------------------------------------ #
#  Tests
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_list_tables(connector: SQLiteConnector) -> None:
    tables = await connector.list_tables()
    assert "users" in tables
    assert "orders" in tables


@pytest.mark.asyncio
async def test_describe_users_table(connector: SQLiteConnector) -> None:
    info = await connector.describe_table("users")
    assert info.name == "users"
    col_names = [c.name for c in info.columns]
    assert "id" in col_names
    assert "name" in col_names
    assert "email" in col_names

    # id should be primary key
    id_col = next(c for c in info.columns if c.name == "id")
    assert id_col.is_primary_key is True


@pytest.mark.asyncio
async def test_get_relationships(connector: SQLiteConnector) -> None:
    rels = await connector.get_relationships()
    assert len(rels) >= 1
    # orders.user_id -> users.id
    rel = next((r for r in rels if r.from_table == "orders"), None)
    assert rel is not None
    assert rel.from_column == "user_id"
    assert rel.to_table == "users"


@pytest.mark.asyncio
async def test_execute_select_query(connector: SQLiteConnector) -> None:
    rows = await connector.execute_query("SELECT * FROM users")
    assert len(rows) == 3
    assert rows[0]["name"] == "Alice"


@pytest.mark.asyncio
async def test_execute_query_with_limit(connector: SQLiteConnector) -> None:
    rows = await connector.execute_query("SELECT * FROM users", limit=2)
    assert len(rows) <= 2


@pytest.mark.asyncio
async def test_get_sample_data(connector: SQLiteConnector) -> None:
    rows = await connector.get_sample_data("orders", limit=2)
    assert len(rows) <= 2
    assert "total" in rows[0]


@pytest.mark.asyncio
async def test_get_schema_map(connector: SQLiteConnector) -> None:
    schema = await connector.get_schema_map()
    assert "users" in schema
    assert "orders" in schema


@pytest.mark.asyncio
async def test_get_db_stats(connector: SQLiteConnector) -> None:
    stats = await connector.get_db_stats()
    assert stats["db_type"] == "sqlite"
    assert "table_row_counts" in stats
    assert stats["table_row_counts"]["users"] == 3
