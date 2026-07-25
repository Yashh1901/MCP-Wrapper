"""
server.py — Main MCP Server

Implements all MCP tools and manages startup/shutdown lifecycle.
Supports both stdio (for local clients) and HTTP/SSE (for remote clients).

MCP Tools:
  - list_connections        → List all configured DB connections
  - list_tables             → List tables for a connection
  - describe_table          → Full table schema
  - get_schema_map          → Full database schema map
  - get_relationships       → FK/reference relationships
  - execute_query           → Run a SELECT query
  - get_sample_data         → Get N sample rows from a table
  - get_db_stats            → Database statistics
  - get_policy_summary      → Show active policy for a connection
  - health_check            → Check all connection health
"""
from __future__ import annotations

import json
from typing import Any

import structlog
from mcp import tool
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import TextContent, Tool

from mcp_db_wrapper.core.config import load_settings
from mcp_db_wrapper.core.policy import PolicyEngine, PolicyViolation
from mcp_db_wrapper.core.registry import ConnectorRegistry
from mcp_db_wrapper.core.schema import SchemaIntrospector

logger = structlog.get_logger(__name__)

# ------------------------------------------------------------------ #
#  Global state (initialized in create_server())
# ------------------------------------------------------------------ #
_registry: ConnectorRegistry | None = None
_policy: PolicyEngine | None = None
_introspector: SchemaIntrospector | None = None


def _get_introspector() -> SchemaIntrospector:
    if _introspector is None:
        raise RuntimeError("Server not initialized. Call create_server() first.")
    return _introspector


def _get_registry() -> ConnectorRegistry:
    if _registry is None:
        raise RuntimeError("Server not initialized.")
    return _registry


def _get_policy() -> PolicyEngine:
    if _policy is None:
        raise RuntimeError("Server not initialized.")
    return _policy


def _ok(data: Any) -> list[TextContent]:
    """Serialize a result dict to a MCP TextContent response."""
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _err(message: str) -> list[TextContent]:
    """Serialize an error to a MCP TextContent response."""
    return [TextContent(type="text", text=json.dumps({"error": message}, indent=2))]


# ------------------------------------------------------------------ #
#  Server factory
# ------------------------------------------------------------------ #

async def create_server() -> Server:
    """
    Create and initialize the MCP server with all tools registered.

    Returns:
        Configured MCP Server instance.
    """
    global _registry, _policy, _introspector

    settings = load_settings()
    _policy = PolicyEngine()
    _registry = ConnectorRegistry()
    await _registry.initialize(eager=False)  # lazy connect
    _introspector = SchemaIntrospector(_registry, _policy)

    server = Server(
        name=settings.server_name,
        version=settings.server_version,
    )

    # ----------------------------------------------------------------
    #  Tool: list_connections
    # ----------------------------------------------------------------
    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return _get_tool_definitions()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        return await _dispatch_tool(name, arguments)

    logger.info(
        "mcp_server_created",
        name=settings.server_name,
        version=settings.server_version,
        transport=settings.transport,
    )
    return server


# ------------------------------------------------------------------ #
#  Tool definitions (metadata)
# ------------------------------------------------------------------ #

def _get_tool_definitions() -> list[Tool]:
    return [
        Tool(
            name="list_connections",
            description=(
                "List all configured database connections with their type, "
                "description, and connection status. Use this first to discover "
                "what databases are available."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="list_tables",
            description=(
                "List all tables (or collections for MongoDB) in a database connection. "
                "Results are filtered by policy — some tables may be hidden. "
                "Use 'list_connections' first to get valid connection names."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "The database connection name (from list_connections).",
                    }
                },
                "required": ["connection"],
            },
        ),
        Tool(
            name="describe_table",
            description=(
                "Get the full schema description of a specific table or collection. "
                "Returns column names, data types, primary keys, foreign keys, "
                "nullability, defaults, and whether a column is masked by policy. "
                "Masked columns exist but their values cannot be read."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "The database connection name.",
                    },
                    "table": {
                        "type": "string",
                        "description": "The table or collection name to describe.",
                    },
                },
                "required": ["connection", "table"],
            },
        ),
        Tool(
            name="get_schema_map",
            description=(
                "Get the complete schema map for an entire database — all visible tables "
                "and their column definitions in one call. Useful for understanding the "
                "full database structure at once. Results are policy-filtered."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "The database connection name.",
                    }
                },
                "required": ["connection"],
            },
        ),
        Tool(
            name="get_relationships",
            description=(
                "Get foreign key relationships between tables in a database. "
                "Shows which tables reference each other, useful for understanding "
                "the data model and crafting JOIN queries. MongoDB returns empty (no FK)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "The database connection name.",
                    }
                },
                "required": ["connection"],
            },
        ),
        Tool(
            name="execute_query",
            description=(
                "Execute a SELECT SQL query on the database. "
                "ONLY SELECT statements are allowed — any other operation (INSERT, UPDATE, "
                "DELETE, DROP, etc.) will be rejected. "
                "Results are limited by policy (max_rows_per_query). "
                "Sensitive columns are automatically masked in the output."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "The database connection name.",
                    },
                    "sql": {
                        "type": "string",
                        "description": (
                            "The SELECT SQL query to execute. "
                            "Example: 'SELECT id, name FROM users WHERE active = true'"
                        ),
                    },
                    "table_hint": {
                        "type": "string",
                        "description": (
                            "Optional: the main table name being queried, "
                            "used to apply column masking correctly."
                        ),
                    },
                },
                "required": ["connection", "sql"],
            },
        ),
        Tool(
            name="execute_mongo_query",
            description=(
                "Execute a MongoDB find() query on a collection. "
                "Supports filter documents and field projections. "
                "Only works with MongoDB connections."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "The MongoDB connection name.",
                    },
                    "collection": {
                        "type": "string",
                        "description": "The collection name to query.",
                    },
                    "filter": {
                        "type": "object",
                        "description": "MongoDB filter document (like a WHERE clause). Default: {} (all docs).",
                        "default": {},
                    },
                    "projection": {
                        "type": "object",
                        "description": "Fields to include (1) or exclude (0). Default: all fields.",
                        "default": {},
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max documents to return. Capped by policy.",
                        "default": 20,
                    },
                },
                "required": ["connection", "collection"],
            },
        ),
        Tool(
            name="get_sample_data",
            description=(
                "Get a small sample of rows from a table to understand the data format. "
                "Results are limited by policy (sample_data_max_rows, usually 3-10 rows). "
                "Sensitive columns are automatically masked."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "The database connection name.",
                    },
                    "table": {
                        "type": "string",
                        "description": "The table or collection name to sample.",
                    },
                },
                "required": ["connection", "table"],
            },
        ),
        Tool(
            name="get_db_stats",
            description=(
                "Get high-level database statistics including table row counts, "
                "database size, version info, and index information."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "The database connection name.",
                    }
                },
                "required": ["connection"],
            },
        ),
        Tool(
            name="get_policy_summary",
            description=(
                "Get the active access control policy for a database connection. "
                "Shows what is allowed/denied, masked tables, and row limits. "
                "Useful for understanding what the AI can and cannot access."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "The database connection name.",
                    }
                },
                "required": ["connection"],
            },
        ),
        Tool(
            name="health_check",
            description=(
                "Check the health of all configured database connections. "
                "Returns status (healthy/unhealthy/not_connected) for each connection."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
    ]


# ------------------------------------------------------------------ #
#  Tool dispatcher
# ------------------------------------------------------------------ #

async def _dispatch_tool(name: str, args: dict[str, Any]) -> list[TextContent]:
    """Route tool calls to the appropriate handler."""
    introspector = _get_introspector()
    registry = _get_registry()
    policy = _get_policy()

    try:
        match name:

            case "list_connections":
                data = registry.list_connections()
                return _ok({"connections": data, "total": len(data)})

            case "list_tables":
                conn = args["connection"]
                result = await introspector.list_tables(conn)
                return _ok(result)

            case "describe_table":
                result = await introspector.describe_table(
                    args["connection"], args["table"]
                )
                return _ok(result)

            case "get_schema_map":
                result = await introspector.get_schema_map(args["connection"])
                return _ok(result)

            case "get_relationships":
                result = await introspector.get_relationships(args["connection"])
                return _ok(result)

            case "execute_query":
                result = await introspector.execute_query(
                    args["connection"],
                    args["sql"],
                    table_hint=args.get("table_hint"),
                )
                return _ok(result)

            case "execute_mongo_query":
                from mcp_db_wrapper.connectors.mongodb import MongoDBConnector

                conn_name = args["connection"]
                policy.assert_query_execution(conn_name)
                policy.assert_table_access(conn_name, args["collection"])

                connector = await registry.get(conn_name)
                if not isinstance(connector, MongoDBConnector):
                    return _err(
                        f"Connection '{conn_name}' is not a MongoDB connection."
                    )
                limit = min(
                    args.get("limit", 20),
                    policy.get_row_limit(conn_name),
                )
                rows = await connector.execute_mongo_query(
                    collection_name=args["collection"],
                    filter_dict=args.get("filter", {}),
                    projection=args.get("projection"),
                    limit=limit,
                )
                rows = policy.apply_column_masks(conn_name, args["collection"], rows)
                return _ok({
                    "connection": conn_name,
                    "collection": args["collection"],
                    "count": len(rows),
                    "documents": rows,
                })

            case "get_sample_data":
                result = await introspector.get_sample_data(
                    args["connection"], args["table"]
                )
                return _ok(result)

            case "get_db_stats":
                result = await introspector.get_db_stats(args["connection"])
                return _ok(result)

            case "get_policy_summary":
                summary = policy.get_policy_summary(args["connection"])
                return _ok(summary)

            case "health_check":
                result = await registry.health_check()
                return _ok({"connections": result})

            case _:
                return _err(f"Unknown tool: '{name}'")

    except PolicyViolation as e:
        logger.warning("policy_violation", tool=name, error=str(e))
        return _err(f"Policy violation: {e}")
    except KeyError as e:
        logger.warning("key_error", tool=name, error=str(e))
        return _err(str(e))
    except Exception as e:
        logger.error("tool_error", tool=name, error=str(e), exc_info=True)
        return _err(f"Error executing '{name}': {e}")


# ------------------------------------------------------------------ #
#  Server shutdown helper
# ------------------------------------------------------------------ #

async def shutdown_server() -> None:
    """Gracefully shut down the server and disconnect all connectors."""
    global _registry
    if _registry:
        await _registry.shutdown()
