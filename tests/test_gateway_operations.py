"""Unit tests for safe self-service gateway operations."""

from __future__ import annotations

import json

from mcp_db_wrapper.cli import _safe_policy_template
from mcp_db_wrapper.core.audit import AuditLogger


def test_generated_policy_is_deny_first() -> None:
    generated = _safe_policy_template("inventory", ["products", "users"])
    policy = generated["policies"]["inventory"]
    assert generated["defaults"]["allow_query_execution"] is False
    assert policy["allow_query_execution"] is False
    assert policy["tables"]["deny"] == ["products", "users"]


def test_audit_log_excludes_sensitive_content(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLogger(str(path)).record(tool="execute_query", connection="inventory", outcome="success")
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["tool"] == "execute_query"
    assert event["connection"] == "inventory"
    assert "sql" not in event
    assert "rows" not in event
