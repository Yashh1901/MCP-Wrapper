"""
connectors/__init__.py — Connector registry and factory
"""
from mcp_db_wrapper.connectors.base import BaseConnector, ColumnInfo, RelationshipInfo, TableInfo
from mcp_db_wrapper.connectors.postgres import PostgresConnector
from mcp_db_wrapper.connectors.mysql import MySQLConnector
from mcp_db_wrapper.connectors.mongodb import MongoDBConnector
from mcp_db_wrapper.connectors.sqlite import SQLiteConnector
from mcp_db_wrapper.connectors.redis import RedisConnector
from mcp_db_wrapper.connectors.mssql import MSSQLConnector
from mcp_db_wrapper.connectors.supabase import SupabaseConnector

__all__ = [
    "BaseConnector",
    "ColumnInfo",
    "RelationshipInfo",
    "TableInfo",
    "PostgresConnector",
    "MySQLConnector",
    "MongoDBConnector",
    "SQLiteConnector",
    "RedisConnector",
    "MSSQLConnector",
    "SupabaseConnector",
]

CONNECTOR_MAP: dict[str, type[BaseConnector]] = {
    "postgres": PostgresConnector,
    "postgresql": PostgresConnector,
    "mysql": MySQLConnector,
    "mariadb": MySQLConnector,
    "mongodb": MongoDBConnector,
    "mongo": MongoDBConnector,
    "sqlite": SQLiteConnector,
    "redis": RedisConnector,
    "mssql": MSSQLConnector,
    "sqlserver": MSSQLConnector,
    "supabase": SupabaseConnector,
}


def get_connector_class(db_type: str) -> type[BaseConnector]:
    """Return the connector class for a given database type string."""
    cls = CONNECTOR_MAP.get(db_type.lower())
    if cls is None:
        supported = sorted(set(CONNECTOR_MAP.keys()))
        raise ValueError(
            f"Unsupported database type: '{db_type}'. "
            f"Supported types: {supported}"
        )
    return cls
