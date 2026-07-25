"""
tests/test_policy.py — Unit tests for the policy engine
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from mcp_db_wrapper.core.policy import PolicyEngine, PolicyViolation


# ------------------------------------------------------------------ #
#  Fixtures
# ------------------------------------------------------------------ #

SAMPLE_POLICY_YAML = """
defaults:
  allow_schema_introspection: true
  allow_query_execution: true
  max_rows_per_query: 100
  allow_sample_data: true
  sample_data_max_rows: 5

policies:
  test_db:
    allow_schema_introspection: true
    allow_query_execution: true
    max_rows_per_query: 50

    tables:
      allow:
        - users
        - products
        - orders

    column_masks:
      users:
        - password_hash
        - email

  locked_db:
    allow_schema_introspection: false
    allow_query_execution: false
    allow_sample_data: false
"""


@pytest.fixture
def policy_file(tmp_path: Path) -> str:
    """Write a sample policy YAML and return its path."""
    p = tmp_path / "policies.yaml"
    p.write_text(SAMPLE_POLICY_YAML)
    return str(p)


@pytest.fixture
def engine(policy_file: str) -> PolicyEngine:
    return PolicyEngine(policy_path=policy_file)


# ------------------------------------------------------------------ #
#  Tests: schema access
# ------------------------------------------------------------------ #

def test_schema_access_allowed(engine: PolicyEngine) -> None:
    engine.assert_schema_access("test_db")  # should not raise


def test_schema_access_denied(engine: PolicyEngine) -> None:
    with pytest.raises(PolicyViolation):
        engine.assert_schema_access("locked_db")


# ------------------------------------------------------------------ #
#  Tests: table access
# ------------------------------------------------------------------ #

def test_table_access_allowed(engine: PolicyEngine) -> None:
    engine.assert_table_access("test_db", "users")


def test_table_access_blocked_by_allowlist(engine: PolicyEngine) -> None:
    with pytest.raises(PolicyViolation):
        engine.assert_table_access("test_db", "admin_secrets")


def test_table_access_case_insensitive(engine: PolicyEngine) -> None:
    engine.assert_table_access("test_db", "USERS")


# ------------------------------------------------------------------ #
#  Tests: query execution
# ------------------------------------------------------------------ #

def test_query_execution_allowed(engine: PolicyEngine) -> None:
    engine.assert_query_execution("test_db")


def test_query_execution_denied(engine: PolicyEngine) -> None:
    with pytest.raises(PolicyViolation):
        engine.assert_query_execution("locked_db")


# ------------------------------------------------------------------ #
#  Tests: filter_tables
# ------------------------------------------------------------------ #

def test_filter_tables_allowlist(engine: PolicyEngine) -> None:
    all_tables = ["users", "products", "orders", "admin_logs", "audit"]
    visible = engine.filter_tables("test_db", all_tables)
    assert visible == ["users", "products", "orders"]


def test_filter_tables_unknown_connection_uses_defaults(engine: PolicyEngine) -> None:
    # no_policy_db not in policies → uses defaults (allow all)
    all_tables = ["table_a", "table_b"]
    visible = engine.filter_tables("no_policy_db", all_tables)
    assert visible == all_tables


# ------------------------------------------------------------------ #
#  Tests: column masking
# ------------------------------------------------------------------ #

def test_apply_column_masks(engine: PolicyEngine) -> None:
    rows = [
        {"id": 1, "name": "Alice", "email": "alice@example.com", "password_hash": "abc123"},
        {"id": 2, "name": "Bob", "email": "bob@example.com", "password_hash": "def456"},
    ]
    masked = engine.apply_column_masks("test_db", "users", rows)
    assert masked[0]["email"] == "***MASKED***"
    assert masked[0]["password_hash"] == "***MASKED***"
    assert masked[0]["name"] == "Alice"   # non-masked
    assert masked[0]["id"] == 1           # non-masked


def test_no_masks_for_unlisted_table(engine: PolicyEngine) -> None:
    rows = [{"id": 1, "secret_col": "sensitive"}]
    masked = engine.apply_column_masks("test_db", "products", rows)
    # products has no masks — data returned as-is
    assert masked[0]["secret_col"] == "sensitive"


# ------------------------------------------------------------------ #
#  Tests: row limit
# ------------------------------------------------------------------ #

def test_enforce_row_limit(engine: PolicyEngine) -> None:
    rows = [{"id": i} for i in range(200)]
    limited = engine.enforce_row_limit("test_db", rows)
    assert len(limited) == 50  # test_db max_rows_per_query = 50


def test_enforce_sample_row_limit(engine: PolicyEngine) -> None:
    rows = [{"id": i} for i in range(50)]
    limited = engine.enforce_row_limit("test_db", rows, is_sample=True)
    assert len(limited) == 5  # default sample_data_max_rows = 5


# ------------------------------------------------------------------ #
#  Tests: policy summary
# ------------------------------------------------------------------ #

def test_policy_summary(engine: PolicyEngine) -> None:
    summary = engine.get_policy_summary("test_db")
    assert summary["allow_query_execution"] is True
    assert summary["max_rows_per_query"] == 50
    assert "users" in summary["masked_tables"]
