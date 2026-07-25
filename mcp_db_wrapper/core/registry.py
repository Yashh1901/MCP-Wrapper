"""
core/registry.py — Connector Registry

Manages the lifecycle of all database connector instances.
Connectors are lazily connected and cached in memory.

Usage:
    registry = ConnectorRegistry()
    await registry.initialize()            # connect all configured DBs
    connector = registry.get("my_postgres")
    tables = await connector.list_tables()
    await registry.shutdown()
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog

from mcp_db_wrapper.connectors import BaseConnector, get_connector_class
from mcp_db_wrapper.core.config import ConnectionConfig, load_connections

logger = structlog.get_logger(__name__)


class ConnectorRegistry:
    """
    Central registry for all database connectors.

    Provides:
      - Lazy connector instantiation
      - Async connection management
      - Health checks
      - Graceful shutdown
    """

    def __init__(self, connections: dict[str, ConnectionConfig] | None = None) -> None:
        self._configs: dict[str, ConnectionConfig] = connections or load_connections()
        self._connectors: dict[str, BaseConnector] = {}
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------- #
    #  Initialization / Shutdown
    # -------------------------------------------------------------- #

    async def initialize(self, eager: bool = False) -> None:
        """
        Initialize the registry.

        Args:
            eager: If True, connect ALL configured databases immediately.
                   If False (default), connectors are connected on first use.
        """
        if eager:
            tasks = [self._connect(name) for name in self._configs]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for name, result in zip(self._configs, results):
                if isinstance(result, Exception):
                    logger.error(
                        "connector_connect_failed",
                        connection=name,
                        error=str(result),
                    )
        logger.info(
            "registry_initialized",
            total=len(self._configs),
            connected=len(self._connectors),
        )

    async def shutdown(self) -> None:
        """Disconnect all active connectors."""
        tasks = [
            conn.disconnect()
            for conn in self._connectors.values()
            if conn.is_connected
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connectors.clear()
        logger.info("registry_shutdown")

    # -------------------------------------------------------------- #
    #  Connector access
    # -------------------------------------------------------------- #

    async def get(self, connection_name: str) -> BaseConnector:
        """
        Get a connected connector by name.

        If the connector has not been connected yet, connects it first.

        Args:
            connection_name: The connection identifier from connections.yaml.

        Returns:
            A connected BaseConnector instance.

        Raises:
            KeyError: If the connection name is not configured.
            ConnectionError: If connection fails.
        """
        if connection_name not in self._configs:
            available = sorted(self._configs.keys())
            raise KeyError(
                f"No connection configured for '{connection_name}'. "
                f"Available: {available}"
            )

        async with self._lock:
            if connection_name not in self._connectors:
                await self._connect(connection_name)
            conn = self._connectors[connection_name]
            if not conn.is_connected:
                await conn.connect()
        return conn

    async def _connect(self, name: str) -> None:
        """Instantiate and connect a connector for the given name."""
        config = self._configs[name]
        cls = get_connector_class(config.type)
        connector = cls(config)
        try:
            await connector.connect()
            self._connectors[name] = connector
        except Exception as e:
            logger.error("connector_connect_error", connection=name, error=str(e))
            raise

    def list_connections(self) -> list[dict[str, Any]]:
        """Return metadata for all configured connections."""
        result = []
        for name, cfg in self._configs.items():
            conn = self._connectors.get(name)
            result.append({
                "name": name,
                "type": cfg.type,
                "description": cfg.description,
                "connected": conn.is_connected if conn else False,
            })
        return result

    async def health_check(self) -> dict[str, Any]:
        """
        Perform a lightweight health check on all active connections.

        Returns:
            Dict mapping connection name -> health status.
        """
        results = {}
        for name, conn in self._connectors.items():
            try:
                # Just try listing tables as a ping
                await conn.list_tables()
                results[name] = {"status": "healthy", "type": conn.DB_TYPE}
            except Exception as e:
                results[name] = {"status": "unhealthy", "error": str(e)}
        # Report unconfigured/unconnected ones
        for name in self._configs:
            if name not in results:
                results[name] = {"status": "not_connected", "type": self._configs[name].type}
        return results
