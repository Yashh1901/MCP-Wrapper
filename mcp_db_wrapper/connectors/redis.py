"""
connectors/redis.py — Redis Connector

Redis is a key-value store, not a relational DB.
Schema introspection = key pattern analysis and namespace mapping.
Query execution = key lookups (GET, HGETALL, LRANGE, etc.)
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import redis.asyncio as aioredis
import structlog

from mcp_db_wrapper.connectors.base import (
    BaseConnector,
    ColumnInfo,
    RelationshipInfo,
    TableInfo,
)
from mcp_db_wrapper.core.config import ConnectionConfig

logger = structlog.get_logger(__name__)

_SCAN_COUNT = 200      # Keys per SCAN iteration
_MAX_KEY_SAMPLE = 100  # Max keys to sample for pattern analysis


class RedisConnector(BaseConnector):
    """
    Redis connector for key-space introspection.

    'Tables' in Redis context = key namespaces (e.g., 'user:*', 'session:*').
    'Columns' = hash fields within a namespace.
    """

    DB_TYPE = "redis"

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._client: aioredis.Redis | None = None

    async def connect(self) -> None:
        url = self.config.get("url", "redis://localhost:6379/0")
        logger.info("redis_connecting", connection=self.name)
        self._client = aioredis.from_url(url, decode_responses=True)
        await self._client.ping()
        self._connected = True
        logger.info("redis_connected", connection=self.name)

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
            self._connected = False

    async def list_tables(self) -> list[str]:
        """Return discovered key namespaces (e.g. 'user', 'session', 'order')."""
        return list((await self._get_namespaces()).keys())

    async def describe_table(self, table_name: str) -> TableInfo:
        """
        Describe a Redis namespace by sampling keys matching 'namespace:*'.

        For hash keys, returns hash fields as 'columns'.
        """
        assert self._client, "Not connected"
        namespaces = await self._get_namespaces()
        sample_keys = namespaces.get(table_name, [])[:5]

        fields: dict[str, set[str]] = defaultdict(set)
        for key in sample_keys:
            key_type = await self._client.type(key)
            if key_type == "hash":
                hdata = await self._client.hgetall(key)
                for field, val in hdata.items():
                    fields[field].add("string")
            elif key_type == "string":
                fields["value"].add("string")
            elif key_type == "list":
                fields["items"].add("array")
            elif key_type == "set":
                fields["members"].add("set")
            elif key_type == "zset":
                fields["score"].add("float")
                fields["member"].add("string")

        columns = [
            ColumnInfo(name=f, data_type=" | ".join(sorted(types)))
            for f, types in fields.items()
        ]

        return TableInfo(
            name=table_name,
            table_type="KEY_NAMESPACE",
            columns=columns,
            comment=f"Redis namespace '{table_name}:*', sampled {len(sample_keys)} keys.",
        )

    async def get_schema_map(self) -> dict[str, TableInfo]:
        tables = await self.list_tables()
        return {t: await self.describe_table(t) for t in tables}

    async def get_relationships(self) -> list[RelationshipInfo]:
        return []  # Redis has no relationships

    async def execute_query(
        self, sql: str, params: list[Any] | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "Redis does not support SQL. Use execute_redis_command() instead."
        )

    async def execute_redis_command(
        self, command: str, *args: Any
    ) -> Any:
        """
        Execute a safe read-only Redis command.

        Allowed commands: GET, HGET, HGETALL, LRANGE, SMEMBERS, ZRANGE,
                          KEYS, SCAN, TYPE, EXISTS, TTL, STRLEN, SCARD, LLEN
        """
        assert self._client, "Not connected"
        ALLOWED = {
            "GET", "HGET", "HGETALL", "LRANGE", "SMEMBERS", "ZRANGE",
            "KEYS", "SCAN", "TYPE", "EXISTS", "TTL", "STRLEN", "SCARD",
            "LLEN", "HKEYS", "HVALS", "HLEN", "ZSCORE", "ZRANK", "MGET",
        }
        if command.upper() not in ALLOWED:
            raise PermissionError(
                f"Redis command '{command}' is not allowed. "
                f"Only read-only commands are permitted: {sorted(ALLOWED)}"
            )
        return await self._client.execute_command(command, *args)

    async def get_sample_data(self, table_name: str, limit: int = 5) -> list[dict[str, Any]]:
        assert self._client, "Not connected"
        namespaces = await self._get_namespaces()
        keys = namespaces.get(table_name, [])[:limit]
        results = []
        for key in keys:
            key_type = await self._client.type(key)
            entry: dict[str, Any] = {"_key": key, "_type": key_type}
            if key_type == "hash":
                entry["data"] = await self._client.hgetall(key)
            elif key_type == "string":
                entry["data"] = await self._client.get(key)
            results.append(entry)
        return results

    async def get_db_stats(self) -> dict[str, Any]:
        assert self._client, "Not connected"
        info = await self._client.info()
        return {
            "db_type": self.DB_TYPE,
            "connection": self.name,
            "redis_version": info.get("redis_version"),
            "used_memory_human": info.get("used_memory_human"),
            "total_keys": info.get("db0", {}).get("keys", "N/A"),
            "connected_clients": info.get("connected_clients"),
            "uptime_seconds": info.get("uptime_in_seconds"),
        }

    # -------------------------------------------------------------- #
    #  Private helpers
    # -------------------------------------------------------------- #

    async def _get_namespaces(self) -> dict[str, list[str]]:
        """Scan keys and group them by namespace prefix (before first ':')."""
        assert self._client, "Not connected"
        namespaces: dict[str, list[str]] = defaultdict(list)
        cursor = 0
        sampled = 0

        while sampled < _MAX_KEY_SAMPLE:
            cursor, keys = await self._client.scan(
                cursor=cursor, count=_SCAN_COUNT
            )
            for key in keys:
                ns = key.split(":")[0] if ":" in key else key
                namespaces[ns].append(key)
                sampled += 1
                if sampled >= _MAX_KEY_SAMPLE:
                    break
            if cursor == 0:
                break

        return dict(namespaces)
