"""
connectors/mongodb.py — MongoDB Connector

Uses motor (async MongoDB driver).
Schema introspection is inferred from document sampling since MongoDB
is schema-less — we sample documents to derive field types.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import motor.motor_asyncio as motor
import structlog
from bson import ObjectId

from mcp_db_wrapper.connectors.base import (
    BaseConnector,
    ColumnInfo,
    RelationshipInfo,
    TableInfo,
)
from mcp_db_wrapper.core.config import ConnectionConfig

logger = structlog.get_logger(__name__)

_SAMPLE_SIZE = 20  # docs sampled per collection for schema inference


def _bson_to_python(obj: Any) -> Any:
    """Convert BSON types to JSON-serializable Python types."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _bson_to_python(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bson_to_python(v) for v in obj]
    return obj


def _infer_type(value: Any) -> str:
    """Infer a human-readable type string from a BSON value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, ObjectId):
        return "ObjectId"
    return type(value).__name__


class MongoDBConnector(BaseConnector):
    """
    MongoDB connector using motor (async).

    Schema introspection is performed by sampling documents and
    inferring field types — MongoDB has no fixed schema.
    """

    DB_TYPE = "mongodb"

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._client: motor.AsyncIOMotorClient | None = None
        self._db: motor.AsyncIOMotorDatabase | None = None

    async def connect(self) -> None:
        url = self.config.get("url", "mongodb://localhost:27017")
        db_name = self.config.get("database", "test")
        logger.info("mongodb_connecting", connection=self.name)
        self._client = motor.AsyncIOMotorClient(url)
        self._db = self._client[db_name]
        # Ping to verify connection
        await self._client.admin.command("ping")
        self._connected = True
        logger.info("mongodb_connected", connection=self.name, db=db_name)

    async def disconnect(self) -> None:
        if self._client:
            self._client.close()
            self._connected = False

    async def list_tables(self) -> list[str]:
        """List all collection names (MongoDB equivalent of tables)."""
        assert self._db is not None, "Not connected"
        return await self._db.list_collection_names()

    async def describe_table(self, table_name: str) -> TableInfo:
        """
        Infer schema from a sample of documents in the collection.

        Returns a TableInfo where columns are inferred from document fields.
        """
        assert self._db is not None, "Not connected"
        collection = self._db[table_name]

        # Sample documents to infer field types
        docs = await collection.find({}).limit(_SAMPLE_SIZE).to_list(length=_SAMPLE_SIZE)

        # Aggregate field types across all sampled docs
        field_types: dict[str, set[str]] = defaultdict(set)
        for doc in docs:
            for field, value in doc.items():
                field_types[field].add(_infer_type(value))

        # Count documents (approximated)
        approx_count = await collection.estimated_document_count()

        columns = [
            ColumnInfo(
                name=field,
                data_type=" | ".join(sorted(types)),
                nullable=True,  # MongoDB fields are always optional
                is_primary_key=(field == "_id"),
                extra={"inferred_from_sample": True},
            )
            for field, types in field_types.items()
        ]

        return TableInfo(
            name=table_name,
            table_type="COLLECTION",
            columns=columns,
            row_count=approx_count,
            comment=f"Schema inferred from {len(docs)} sampled documents.",
        )

    async def get_schema_map(self) -> dict[str, TableInfo]:
        collections = await self.list_tables()
        return {c: await self.describe_table(c) for c in collections}

    async def get_relationships(self) -> list[RelationshipInfo]:
        """
        MongoDB has no formal FK relationships.
        Returns empty list (references are application-level).
        """
        return []

    async def execute_query(
        self,
        sql: str,
        params: list[Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        MongoDB does not support SQL. This executes a simple find() on a collection.

        For MongoDB, the 'sql' parameter is expected to be in the format:
            db.<collection>.find(<json_filter>)
        Or simply the collection name for a full scan.

        Proper MongoDB query interface is via get_sample_data().
        """
        raise NotImplementedError(
            "MongoDB does not support SQL queries. "
            "Use execute_mongo_query() with a collection name and filter dict, "
            "or use get_sample_data() to browse documents."
        )

    async def execute_mongo_query(
        self,
        collection_name: str,
        filter_dict: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
        limit: int = 50,
        sort: list[tuple[str, int]] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Execute a MongoDB find() query.

        Args:
            collection_name: Name of the collection.
            filter_dict: MongoDB filter document (like WHERE clause).
            projection: Fields to include/exclude.
            limit: Max documents to return.
            sort: List of (field, direction) pairs.

        Returns:
            List of document dicts.
        """
        assert self._db is not None, "Not connected"
        collection = self._db[collection_name]
        cursor = collection.find(filter_dict or {}, projection or {})
        if sort:
            cursor = cursor.sort(sort)
        cursor = cursor.limit(limit)
        docs = await cursor.to_list(length=limit)
        return [_bson_to_python(doc) for doc in docs]

    async def get_sample_data(self, table_name: str, limit: int = 5) -> list[dict[str, Any]]:
        return await self.execute_mongo_query(table_name, limit=limit)

    async def get_db_stats(self) -> dict[str, Any]:
        assert self._db is not None, "Not connected"
        stats = await self._db.command("dbStats")
        collections = await self.list_tables()
        return {
            "db_type": self.DB_TYPE,
            "connection": self.name,
            "database": stats.get("db"),
            "collections": len(collections),
            "objects": stats.get("objects"),
            "data_size_bytes": stats.get("dataSize"),
            "storage_size_bytes": stats.get("storageSize"),
            "indexes": stats.get("indexes"),
            "index_size_bytes": stats.get("indexSize"),
        }
