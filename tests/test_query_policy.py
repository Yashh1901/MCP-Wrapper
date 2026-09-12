"""Regression tests for policy enforcement in raw SQL execution."""

from __future__ import annotations

import pytest

from mcp_db_wrapper.connectors.sqlite import SQLiteConnector
from mcp_db_wrapper.core.config import ConnectionConfig
from mcp_db_wrapper.core.policy import PolicyEngine, PolicyViolation
from mcp_db_wrapper.core.registry import ConnectorRegistry
from mcp_db_wrapper.core.schema import SchemaIntrospector

POLICY = """
defaults:
  allow_schema_introspection: true
  allow_query_execution: true
  max_rows_per_query: 2
policies:
  demo:
    tables:
      allow: [users]
    column_masks:
      users: [email]
"""


@pytest.fixture
async def introspector(tmp_path):
    db = tmp_path / "demo.sqlite"
    connector = SQLiteConnector(ConnectionConfig("demo", {"type": "sqlite", "path": str(db)}))
    await connector.connect()
    assert connector._conn is not None
    await connector._conn.execute("CREATE TABLE users (id INTEGER, email TEXT)")
    await connector._conn.execute("CREATE TABLE secrets (id INTEGER, value TEXT)")
    await connector._conn.executemany(
        "INSERT INTO users VALUES (?, ?)",
        [(1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")],
    )
    await connector._conn.execute("INSERT INTO secrets VALUES (1, 'never expose')")
    await connector._conn.commit()
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(POLICY, encoding="utf-8")
    registry = ConnectorRegistry(
        {"demo": ConnectionConfig("demo", {"type": "sqlite", "path": str(db)})}
    )
    yield SchemaIntrospector(registry, PolicyEngine(str(policy_file)))
    await registry.shutdown()
    await connector.disconnect()


@pytest.mark.asyncio
async def test_query_checks_every_referenced_table(introspector):
    with pytest.raises(PolicyViolation, match="allowlist"):
        await introspector.execute_query(
            "demo", "SELECT u.id FROM users u JOIN secrets s ON s.id = u.id"
        )


@pytest.mark.asyncio
async def test_query_masks_without_untrusted_table_hint(introspector):
    result = await introspector.execute_query("demo", "SELECT id, email FROM users")
    assert result["rows"][0]["email"] == "***MASKED***"


@pytest.mark.asyncio
async def test_explicit_limit_cannot_bypass_policy_cap(introspector):
    result = await introspector.execute_query("demo", "SELECT id FROM users LIMIT 999")
    assert result["row_count"] == 2
