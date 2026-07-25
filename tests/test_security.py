"""
tests/test_security.py — Unit tests for SQL security validation
"""
from __future__ import annotations

import pytest
from mcp_db_wrapper.core.security import QuerySecurityError, QueryValidator


@pytest.fixture
def validator() -> QueryValidator:
    return QueryValidator()


# ------------------------------------------------------------------ #
#  Valid SELECT queries
# ------------------------------------------------------------------ #

def test_simple_select(validator: QueryValidator) -> None:
    sql = validator.validate("SELECT id, name FROM users")
    assert "SELECT" in sql.upper()


def test_select_with_where(validator: QueryValidator) -> None:
    validator.validate("SELECT * FROM orders WHERE status = 'active'")


def test_select_with_join(validator: QueryValidator) -> None:
    validator.validate(
        "SELECT u.id, u.name, o.total "
        "FROM users u JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 100"
    )


def test_select_with_limit(validator: QueryValidator) -> None:
    validator.validate("SELECT * FROM products LIMIT 10")


def test_select_with_subquery(validator: QueryValidator) -> None:
    validator.validate(
        "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders WHERE total > 50)"
    )


def test_select_aggregate(validator: QueryValidator) -> None:
    validator.validate(
        "SELECT category, COUNT(*) AS cnt FROM products GROUP BY category ORDER BY cnt DESC"
    )


def test_trailing_semicolon_stripped(validator: QueryValidator) -> None:
    sql = validator.validate("SELECT 1;")
    assert not sql.endswith(";")


# ------------------------------------------------------------------ #
#  Invalid / dangerous queries
# ------------------------------------------------------------------ #

def test_empty_query(validator: QueryValidator) -> None:
    with pytest.raises(QuerySecurityError):
        validator.validate("")


def test_insert_rejected(validator: QueryValidator) -> None:
    with pytest.raises(QuerySecurityError):
        validator.validate("INSERT INTO users (name) VALUES ('hacker')")


def test_update_rejected(validator: QueryValidator) -> None:
    with pytest.raises(QuerySecurityError):
        validator.validate("UPDATE users SET role = 'admin' WHERE id = 1")


def test_delete_rejected(validator: QueryValidator) -> None:
    with pytest.raises(QuerySecurityError):
        validator.validate("DELETE FROM users WHERE 1=1")


def test_drop_rejected(validator: QueryValidator) -> None:
    with pytest.raises(QuerySecurityError):
        validator.validate("DROP TABLE users")


def test_truncate_rejected(validator: QueryValidator) -> None:
    with pytest.raises(QuerySecurityError):
        validator.validate("TRUNCATE TABLE orders")


def test_create_rejected(validator: QueryValidator) -> None:
    with pytest.raises(QuerySecurityError):
        validator.validate("CREATE TABLE malicious (id INT)")


def test_multiple_statements_rejected(validator: QueryValidator) -> None:
    with pytest.raises(QuerySecurityError):
        validator.validate("SELECT 1; DROP TABLE users")


def test_exec_rejected(validator: QueryValidator) -> None:
    with pytest.raises(QuerySecurityError):
        validator.validate("EXEC xp_cmdshell('dir')")


def test_sleep_injection_rejected(validator: QueryValidator) -> None:
    with pytest.raises(QuerySecurityError):
        validator.validate("SELECT * FROM users WHERE 1=1; WAITFOR DELAY '0:0:5'")
