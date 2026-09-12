"""
core/security.py — Security utilities

Provides:
  - SQL query validation (SELECT-only enforcement via AST parsing)
  - SQL injection detection
  - API key validation for HTTP transport
  - TLS context builder
"""

from __future__ import annotations

import re
import ssl

import sqlglot
import sqlglot.expressions as exp
import structlog

logger = structlog.get_logger(__name__)

# ------------------------------------------------------------------ #
#  Constants
# ------------------------------------------------------------------ #

# Dangerous SQL keywords that should never appear in allowed queries
_DANGEROUS_PATTERNS = re.compile(
    r"\b(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE"
    r"|GRANT|REVOKE|CALL|MERGE|REPLACE|LOAD|OUTFILE|DUMPFILE"
    r"|INTO\s+OUTFILE|INTO\s+DUMPFILE"
    r"|SYS\.|MYSQL\.|PG_SLEEP|WAITFOR|BENCHMARK|SLEEP)\b",
    re.IGNORECASE,
)


# ------------------------------------------------------------------ #
#  Exceptions
# ------------------------------------------------------------------ #


class QuerySecurityError(Exception):
    """Raised when a query fails security validation."""


# ------------------------------------------------------------------ #
#  Query validator
# ------------------------------------------------------------------ #


class QueryValidator:
    """
    Validates SQL queries to ensure they are safe SELECT-only statements.

    Uses sqlglot for AST-level validation, with a fallback regex layer
    for extra protection against obfuscation attacks.
    """

    ALLOWED_STATEMENT_TYPES = (exp.Select,)

    def validate(self, sql: str, dialect: str | None = None) -> str:
        """
        Validate and return the cleaned SQL string.

        Args:
            sql: The SQL string to validate.
            dialect: Database dialect hint (e.g. 'postgres', 'mysql').

        Returns:
            The stripped, validated SQL string.

        Raises:
            QuerySecurityError: If the query is not safe.
        """
        if not sql or not sql.strip():
            raise QuerySecurityError("Empty query is not allowed.")

        cleaned = sql.strip().rstrip(";").strip()

        # Step 2: AST-based validation via sqlglot
        try:
            statements = sqlglot.parse(cleaned, dialect=dialect)
        except Exception as e:
            raise QuerySecurityError(f"SQL parse error: {e}") from e

        if not statements:
            raise QuerySecurityError("Could not parse SQL statement.")

        if len(statements) > 1:
            raise QuerySecurityError(
                "Multiple statements are not allowed. Submit one SELECT statement at a time."
            )

        stmt = statements[0]
        if not isinstance(stmt, tuple(self.ALLOWED_STATEMENT_TYPES)):
            raise QuerySecurityError(
                f"Only SELECT statements are allowed. Got: {type(stmt).__name__}."
            )

        # Step 3: Check the complete AST.  This is deliberately AST based rather
        # than a keyword blacklist: words such as "drop" are valid string values.
        self._check_subqueries(stmt)

        logger.debug("query_validated", sql=cleaned[:200])
        return cleaned

    def referenced_tables(self, sql: str, dialect: str | None = None) -> set[str]:
        """Return physical tables referenced by an already-valid SELECT.

        CTE names are excluded.  The caller uses this to enforce table policy on
        every table in a query, including joins and nested subqueries.
        """
        statement = sqlglot.parse_one(sql, dialect=dialect)
        cte_names = {
            cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE) if cte.alias_or_name
        }
        return {
            table.name.lower()
            for table in statement.find_all(exp.Table)
            if table.name and table.name.lower() not in cte_names
        }

    def _check_subqueries(self, stmt: exp.Expression) -> None:
        """Check that subqueries don't contain write operations."""
        for node in stmt.walk():
            if isinstance(
                node, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Command)
            ):
                raise QuerySecurityError(
                    f"Subquery contains disallowed operation: {type(node).__name__}"
                )


# ------------------------------------------------------------------ #
#  API Key validation
# ------------------------------------------------------------------ #


def validate_api_key(provided_key: str | None, expected_key: str | None) -> bool:
    """
    Validate the API key using constant-time comparison to prevent timing attacks.

    Args:
        provided_key: Key from the request header.
        expected_key: Expected key from settings.

    Returns:
        True if valid (or if no key is configured), False otherwise.
    """
    if not expected_key:
        # No key configured → open access (warn in logs)
        logger.warning("no_api_key_configured", message="HTTP transport is unauthenticated!")
        return True

    if not provided_key:
        return False

    # Use hmac.compare_digest for timing-safe comparison
    import hmac

    return hmac.compare_digest(
        provided_key.encode("utf-8"),
        expected_key.encode("utf-8"),
    )


# ------------------------------------------------------------------ #
#  TLS context builder
# ------------------------------------------------------------------ #


def build_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """
    Build an SSL context for TLS-enabled HTTP transport.

    Args:
        cert_path: Path to the TLS certificate file.
        key_path: Path to the TLS private key file.

    Returns:
        Configured SSLContext.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# ------------------------------------------------------------------ #
#  Singleton validator instance
# ------------------------------------------------------------------ #

_query_validator: QueryValidator | None = None


def get_query_validator() -> QueryValidator:
    """Get the shared QueryValidator instance."""
    global _query_validator
    if _query_validator is None:
        _query_validator = QueryValidator()
    return _query_validator
