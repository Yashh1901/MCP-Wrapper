"""
connectors/supabase.py — Supabase Connector

Supabase is a PostgreSQL-backed BaaS. We connect via the direct
PostgreSQL URL for full schema introspection (bypassing the HTTP API).
The Supabase Python client is used for auth/metadata queries.
"""
from __future__ import annotations

from typing import Any

import structlog
from supabase import AsyncClient, acreate_client

from mcp_db_wrapper.connectors.base import RelationshipInfo, TableInfo
from mcp_db_wrapper.connectors.postgres import PostgresConnector
from mcp_db_wrapper.core.config import ConnectionConfig

logger = structlog.get_logger(__name__)


class SupabaseConnector(PostgresConnector):
    """
    Supabase connector.

    Inherits PostgreSQL connector for full schema introspection via the
    direct db_url connection. Optionally uses the Supabase client for
    auth-gated table access.
    """

    DB_TYPE = "supabase"

    def __init__(self, config: ConnectionConfig) -> None:
        # Supabase uses PostgreSQL under the hood — reuse postgres connector
        # but override the DSN to use the Supabase DB URL
        super().__init__(config)
        self._supabase_client: AsyncClient | None = None
        self._supabase_url: str = config.get("url", "")
        self._supabase_key: str = config.get("key", "")

    def _build_dsn(self) -> str:
        """Use the dedicated db_url for direct Postgres access."""
        if db_url := self.config.get("db_url"):
            return db_url
        raise ValueError(
            f"Supabase connection '{self.name}' requires 'db_url' "
            "(direct PostgreSQL connection string) for schema introspection."
        )

    async def connect(self) -> None:
        """Connect both via direct Postgres (schema) and Supabase client (metadata)."""
        await super().connect()  # establishes asyncpg pool
        if self._supabase_url and self._supabase_key:
            try:
                self._supabase_client = await acreate_client(
                    self._supabase_url, self._supabase_key
                )
                logger.info("supabase_client_connected", connection=self.name)
            except Exception as e:
                logger.warning("supabase_client_failed", error=str(e))

    async def disconnect(self) -> None:
        await super().disconnect()
        # supabase async client doesn't require explicit close

    async def get_db_stats(self) -> dict[str, Any]:
        stats = await super().get_db_stats()
        stats["db_type"] = self.DB_TYPE
        stats["supabase_url"] = self._supabase_url
        return stats
