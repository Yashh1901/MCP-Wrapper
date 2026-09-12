# MCP DB Wrapper — Technical Documentation

> A comprehensive technical reference for the Universal MCP Database Wrapper.  
> Written for engineers, reviewers, and contributors who need to understand the system without prior context.

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Problem Statement](#2-problem-statement)
3. [Architecture Overview](#3-architecture-overview)
4. [Project Structure](#4-project-structure)
5. [Core Concepts](#5-core-concepts)
   - 5.1 [The Model Context Protocol (MCP)](#51-the-model-context-protocol-mcp)
   - 5.2 [Tools](#52-tools)
   - 5.3 [Transports](#53-transports)
6. [Module-by-Module Walkthrough](#6-module-by-module-walkthrough)
   - 6.1 [Configuration Layer (`core/config.py`)](#61-configuration-layer)
   - 6.2 [Connector Abstraction (`connectors/`)](#62-connector-abstraction)
   - 6.3 [Connector Registry (`core/registry.py`)](#63-connector-registry)
   - 6.4 [Security Engine (`core/security.py`)](#64-security-engine)
   - 6.5 [Policy Engine (`core/policy.py`)](#65-policy-engine)
   - 6.6 [Schema Introspector (`core/schema.py`)](#66-schema-introspector)
   - 6.7 [MCP Server (`server.py`)](#67-mcp-server)
   - 6.8 [Transport Layer (`transport/`)](#68-transport-layer)
   - 6.9 [CLI (`cli.py`)](#69-cli)
7. [Request Lifecycle — End to End](#7-request-lifecycle--end-to-end)
8. [Security Architecture](#8-security-architecture)
9. [Configuration Reference](#9-configuration-reference)
   - 9.1 [Environment Variables](#91-environment-variables)
   - 9.2 [Connection Configuration (YAML)](#92-connection-configuration-yaml)
   - 9.3 [Policy Configuration (YAML)](#93-policy-configuration-yaml)
10. [Supported Databases](#10-supported-databases)
11. [Deployment](#11-deployment)
    - 11.1 [Local Development](#111-local-development)
    - 11.2 [Docker](#112-docker)
    - 11.3 [Integrating with AI Clients](#113-integrating-with-ai-clients)
12. [Testing](#12-testing)
13. [Design Decisions & Trade-offs](#13-design-decisions--trade-offs)
14. [Glossary](#14-glossary)

---

## 1. Introduction

MCP DB Wrapper is a Python server that acts as a **secure, read-only bridge between databases and AI tools**. It implements the [Model Context Protocol (MCP)](https://modelcontextprotocol.io), an open standard that defines how Large Language Models (LLMs) discover and invoke external tools.

In practical terms, an LLM such as ChatGPT, Claude, Gemini, or Cursor IDE can connect to this server and — through structured tool calls — list your database tables, inspect column schemas, explore foreign-key relationships, and execute read-only SQL queries, all while respecting fine-grained access control policies you define in a single YAML file.

The server supports **seven database engines** (PostgreSQL, MySQL, SQLite, MongoDB, Redis, MSSQL, and Supabase) and exposes them through a uniform interface. It ships with two transport modes: **stdio** for local IDE integrations (Cursor, Claude Desktop) and **HTTP/SSE** for remote or cloud-based AI clients.

---

## 2. Problem Statement

Modern development workflows increasingly rely on AI assistants to write queries, debug schemas, and reason about data relationships. However, connecting an LLM directly to a production database introduces three categories of risk:

| Risk | Description |
|---|---|
| **Data Mutation** | An LLM might issue an `INSERT`, `UPDATE`, `DELETE`, or `DROP` statement, corrupting or destroying data. |
| **Data Exfiltration** | Sensitive columns (passwords, SSNs, credit cards) might be returned in LLM context windows and logged, cached, or leaked. |
| **Unbounded Queries** | A `SELECT *` on a billion-row table without a `LIMIT` could exhaust memory and crash the database. |

MCP DB Wrapper eliminates all three risks:

1. **Read-only enforcement**: Every SQL query is parsed into an Abstract Syntax Tree (AST) using `sqlglot`. Only `SELECT` statements pass validation. Write operations are rejected at the AST level before they ever reach the database.

2. **Column masking**: Sensitive columns are replaced with `***MASKED***` in query results. The LLM sees the column exists (for schema awareness) but cannot read the actual values.

3. **Row limits**: Every query result is truncated to a configurable maximum (e.g., 50 rows), preventing runaway scans.

---

## 3. Architecture Overview

The system is composed of five distinct layers. Each layer has a single responsibility, and data flows strictly downward through the stack:

```
┌─────────────────────────────────────────────────────────────────────┐
│                         AI Client                                   │
│          (ChatGPT, Claude, Cursor IDE, Gemini, etc.)                │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  MCP Protocol (JSON-RPC)
                               │
┌──────────────────────────────▼──────────────────────────────────────┐
│  Layer 1: TRANSPORT                                                 │
│  ┌──────────────┐    ┌──────────────────────────┐                   │
│  │ stdio        │    │ HTTP/SSE (FastAPI)        │                   │
│  │ (local IDE)  │    │ (remote/cloud clients)    │                   │
│  └──────┬───────┘    └────────────┬──────────────┘                   │
│         └────────────┬────────────┘                                  │
└──────────────────────┼──────────────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────────────┐
│  Layer 2: MCP SERVER (server.py)                                    │
│  Registers 11 MCP tools, dispatches tool calls, serializes results  │
└──────────────────────┬──────────────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────────────┐
│  Layer 3: SCHEMA INTROSPECTOR (core/schema.py)                      │
│  Orchestrates policy checks + connector calls + column masking      │
└────────────┬─────────────────────────┬──────────────────────────────┘
             │                         │
┌────────────▼──────────┐   ┌──────────▼─────────────┐
│  Layer 4a: POLICY     │   │  Layer 4b: SECURITY    │
│  ENGINE               │   │  ENGINE                │
│  (core/policy.py)     │   │  (core/security.py)    │
│                       │   │                        │
│  - Table allowlists   │   │  - AST-level SQL       │
│  - Table blocklists   │   │    validation          │
│  - Column masking     │   │  - Regex pre-filter    │
│  - Row limits         │   │  - Subquery scanning   │
│  - Sample limits      │   │  - API key validation  │
└────────────┬──────────┘   └────────────────────────┘
             │
┌────────────▼────────────────────────────────────────────────────────┐
│  Layer 5: CONNECTORS (connectors/)                                  │
│  ┌──────────┐ ┌───────┐ ┌────────┐ ┌───────┐ ┌─────┐ ┌──────────┐ │
│  │ Postgres │ │ MySQL │ │ SQLite │ │ Mongo │ │Redis│ │ MSSQL    │ │
│  │ (asyncpg)│ │(aio-  │ │(aio-   │ │(motor)│ │(aio │ │(aioodbc) │ │
│  │          │ │ mysql)│ │ sqlite)│ │       │ │redis│ │          │ │
│  └──────────┘ └───────┘ └────────┘ └───────┘ └─────┘ └──────────┘ │
│                                                       ┌──────────┐ │
│                                                       │ Supabase │ │
│                                                       │(inherits │ │
│                                                       │ Postgres)│ │
│                                                       └──────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

**Key design principle**: The Schema Introspector (Layer 3) is the **only** component that the MCP tools call. It sits between the server and the raw connectors, ensuring that every database interaction passes through both the Policy Engine and the Security Engine before reaching the database driver.

---

## 4. Project Structure

```
MCP Wrapper/
├── mcp_db_wrapper/                  # Main Python package
│   ├── __init__.py                  # Package version (__version__)
│   ├── __main__.py                  # `python -m mcp_db_wrapper` entrypoint
│   ├── cli.py                       # Typer CLI (serve, test-conn, validate, etc.)
│   ├── server.py                    # MCP tool definitions & dispatcher
│   │
│   ├── core/                        # Business logic layer
│   │   ├── __init__.py              # Lazy imports (breaks circular dependency)
│   │   ├── config.py                # Settings, YAML loading, env interpolation
│   │   ├── policy.py                # Policy engine (allowlists, masks, limits)
│   │   ├── registry.py              # Connector lifecycle manager
│   │   ├── schema.py                # High-level schema introspection facade
│   │   └── security.py              # SQL validator, API key auth, TLS
│   │
│   ├── connectors/                  # Database driver implementations
│   │   ├── __init__.py              # Connector factory & CONNECTOR_MAP
│   │   ├── base.py                  # Abstract BaseConnector + data models
│   │   ├── sqlite.py                # SQLite (aiosqlite)
│   │   ├── postgres.py              # PostgreSQL (asyncpg)
│   │   ├── mysql.py                 # MySQL/MariaDB (aiomysql)
│   │   ├── mongodb.py               # MongoDB (motor)
│   │   ├── redis.py                 # Redis (redis-py async)
│   │   ├── mssql.py                 # MSSQL (aioodbc)
│   │   └── supabase.py              # Supabase (extends PostgresConnector)
│   │
│   └── transport/                   # Network layer
│       ├── __init__.py
│       ├── stdio_transport.py       # stdin/stdout JSON-RPC
│       └── http_transport.py        # FastAPI + SSE
│
├── config/
│   └── connections.example.yaml     # Database connection definitions
│
├── policies/
│   └── policies.example.yaml        # Access control policies
│
├── tests/
│   ├── __init__.py
│   ├── test_policy.py               # Policy engine unit tests (14 tests)
│   ├── test_security.py             # SQL validator unit tests (11 tests)
│   ├── test_sqlite_connector.py     # SQLite connector tests (8 tests)
│   └── test_e2e_real_db.py          # Full E2E integration suite (60 tests)
│
├── .env.example                     # Environment variable template
├── pyproject.toml                   # Build config, dependencies, scripts
├── Dockerfile                       # Production container image
├── docker-compose.yml               # Single-command deployment
└── README.md                        # User-facing documentation
```

---

## 5. Core Concepts

### 5.1 The Model Context Protocol (MCP)

MCP is an open protocol, developed by Anthropic and adopted across the AI industry, that standardizes how LLMs interact with external tools. It uses JSON-RPC 2.0 as its wire format.

The protocol defines two roles:

- **MCP Client**: The AI tool (e.g., ChatGPT, Claude Desktop, Cursor IDE). It discovers available tools and invokes them.
- **MCP Server**: The service that advertises tools and handles invocations. **This project is an MCP server.**

The lifecycle works as follows:

1. The client connects and calls `tools/list` to discover available tools.
2. The client calls `tools/call` with a tool name and arguments (e.g., `list_tables` with `{"connection": "my_postgres"}`).
3. The server executes the tool and returns a `TextContent` response (JSON-serialized results).

### 5.2 Tools

MCP DB Wrapper registers **11 tools** that the LLM can call:

| Tool Name | Purpose | Arguments |
|---|---|---|
| `list_connections` | Discover available databases | _(none)_ |
| `list_tables` | List tables in a database (policy-filtered) | `connection` |
| `describe_table` | Get column schema for a table | `connection`, `table` |
| `get_schema_map` | Get the full schema in one call | `connection` |
| `get_relationships` | Get foreign key relationships | `connection` |
| `execute_query` | Run a validated SELECT query | `connection`, `sql`, `table_hint?` |
| `execute_mongo_query` | Run a MongoDB `find()` query | `connection`, `collection`, `filter?`, `projection?`, `limit?` |
| `get_sample_data` | Preview N rows from a table | `connection`, `table` |
| `get_db_stats` | Get row counts, sizes, versions | `connection` |
| `get_policy_summary` | Show what the AI can/cannot access | `connection` |
| `health_check` | Ping all databases | _(none)_ |

### 5.3 Transports

The MCP protocol is transport-agnostic. This server supports two:

| Transport | Module | Use Case | Wire Format |
|---|---|---|---|
| **stdio** | `stdio_transport.py` | Local IDEs (Cursor, Claude Desktop) | stdin/stdout JSON-RPC |
| **HTTP/SSE** | `http_transport.py` | Remote clients, web apps, cloud AI | HTTP POST + Server-Sent Events |

Both transports share the same MCP server instance and tool implementations. The transport layer is purely a communication adapter.

---

## 6. Module-by-Module Walkthrough

### 6.1 Configuration Layer

**File**: `core/config.py`

This module is the foundation that every other module depends on. It handles three concerns:

**a) Application Settings (`Settings` class)**

A `pydantic-settings` model that loads configuration from environment variables with the `MCP_` prefix:

```python
class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    transport: str = "both"        # stdio | http | both
    api_key: str | None = None     # Bearer token for HTTP auth
    policy_path: str = "policies/policies.yaml"
    connections_path: str = "config/connections.yaml"
    # ... more fields
```

Values are resolved in priority order: environment variable → `.env` file → default value. The `Settings` instance is cached as a module-level singleton.

**b) YAML Loading with Environment Variable Interpolation**

The function `_load_yaml()` loads a YAML file and recursively replaces `${ENV_VAR}` placeholders with actual environment variable values. This allows database passwords and other secrets to live in `.env` rather than in version-controlled YAML:

```yaml
# connections.yaml
connections:
  my_postgres:
    type: postgres
    password: "${POSTGRES_PASSWORD}"   # ← resolved at load time
```

If the primary YAML file (e.g., `connections.yaml`) is missing, the loader automatically falls back to the `.example` variant (`connections.example.yaml`), making first-time setup smoother.

**c) Connection Config (`ConnectionConfig` class)**

A lightweight wrapper around the raw YAML dict for each database connection:

```python
class ConnectionConfig:
    name: str           # e.g., "my_postgres"
    type: str           # e.g., "postgres"
    description: str    # e.g., "Main production database"
    raw: dict           # Full YAML dict (host, port, password, etc.)
```

The `raw` dict is passed directly to the connector class, which extracts whatever fields it needs (host, port, SSL mode, connection pool size, etc.).

---

### 6.2 Connector Abstraction

**Directory**: `connectors/`

#### The Abstract Base: `BaseConnector`

Every database driver implements the `BaseConnector` abstract class, which defines a uniform interface:

```python
class BaseConnector(ABC):
    DB_TYPE: str = "unknown"

    # Lifecycle
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    # Schema introspection
    async def list_tables(self) -> list[str]: ...
    async def describe_table(self, table_name: str) -> TableInfo: ...
    async def get_schema_map(self) -> dict[str, TableInfo]: ...
    async def get_relationships(self) -> list[RelationshipInfo]: ...

    # Query execution
    async def execute_query(self, sql: str, params=None, limit=100) -> list[dict]: ...

    # Utilities
    async def get_sample_data(self, table_name: str, limit=5) -> list[dict]: ...
    async def get_db_stats(self) -> dict: ...
```

Every connector also supports the async context manager protocol (`async with PostgresConnector(config) as conn:`).

#### Data Models

Three simple classes describe database schemas:

| Class | Description | Key Fields |
|---|---|---|
| `ColumnInfo` | A single column | `name`, `data_type`, `nullable`, `is_primary_key`, `is_foreign_key`, `foreign_key_ref`, `masked` |
| `TableInfo` | A table or collection | `name`, `schema`, `columns: list[ColumnInfo]`, `row_count`, `table_type` |
| `RelationshipInfo` | A foreign key | `from_table`, `from_column`, `to_table`, `to_column`, `constraint_name` |

These are plain Python classes (not ORM models) with a `to_dict()` method for JSON serialization.

#### Connector Implementations

| Connector | Driver | Schema Source | Notes |
|---|---|---|---|
| `PostgresConnector` | `asyncpg` | `information_schema` + `pg_class` | Connection pooling via `asyncpg.Pool`. Approximate row counts from `pg_class.reltuples`. |
| `MySQLConnector` | `aiomysql` | `INFORMATION_SCHEMA` | Connection pooling via `aiomysql.Pool`. Row counts from `TABLE_ROWS`. |
| `SQLiteConnector` | `aiosqlite` | `PRAGMA table_info`, `PRAGMA foreign_key_list` | Single-file database. No connection pool needed. |
| `MongoDBConnector` | `motor` (async pymongo) | Sampling-based inference | Schema-less: samples 20 documents per collection and infers field types. Returns `COLLECTION` as `table_type`. `execute_query()` raises `NotImplementedError` — use `execute_mongo_query()` instead. |
| `RedisConnector` | `redis-py` (async) | Key pattern scanning | Key namespaces (prefix before `:`) serve as "tables". Hash fields serve as "columns". Only read-only commands are allowed (GET, HGETALL, LRANGE, etc.). |
| `MSSQLConnector` | `aioodbc` | `INFORMATION_SCHEMA` | Connection pooling via `aioodbc.Pool`. Uses `SELECT TOP N` instead of `LIMIT N`. |
| `SupabaseConnector` | Inherits `PostgresConnector` + `supabase-py` | `information_schema` (via direct PostgreSQL URL) | Connects to Supabase's underlying PostgreSQL via `db_url`. The Supabase client is optionally initialized for metadata. |

#### Connector Factory

The `connectors/__init__.py` module maintains a `CONNECTOR_MAP` that maps type strings to connector classes:

```python
CONNECTOR_MAP = {
    "postgres": PostgresConnector,
    "postgresql": PostgresConnector,
    "mysql": MySQLConnector,
    "mariadb": MySQLConnector,
    "sqlite": SQLiteConnector,
    # ... etc.
}
```

The function `get_connector_class("postgres")` returns `PostgresConnector`. This factory is used by the Registry to instantiate connectors dynamically based on the YAML `type` field.

---

### 6.3 Connector Registry

**File**: `core/registry.py`

The `ConnectorRegistry` manages the lifecycle of all database connectors:

```python
class ConnectorRegistry:
    async def initialize(self, eager=False)    # Connect all (or defer)
    async def get(self, connection_name)        # Get a connected connector (lazy-connect)
    async def shutdown()                        # Disconnect all
    def list_connections()                      # Metadata for all configs
    async def health_check()                    # Ping all active connectors
```

**Lazy connection**: By default, connectors are not connected until the first time `get()` is called for that connection name. This avoids connecting to databases that the LLM never queries.

**Thread safety**: An `asyncio.Lock` guards the connector cache to prevent race conditions when two concurrent tool calls try to connect to the same database simultaneously.

**Health checks**: The `health_check()` method calls `list_tables()` on each active connector as a lightweight ping. Connectors that haven't been connected yet are reported as `not_connected`.

---

### 6.4 Security Engine

**File**: `core/security.py`

The security engine provides three functions:

#### a) SQL Query Validation (`QueryValidator`)

This is the most critical security component. Every SQL query submitted by an LLM passes through a three-stage validation pipeline:

**Stage 1: Regex Pre-filter**

A compiled regular expression scans for dangerous SQL keywords before parsing:

```python
_DANGEROUS_PATTERNS = re.compile(
    r"\b(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE"
    r"|GRANT|REVOKE|CALL|MERGE|REPLACE|LOAD|OUTFILE|DUMPFILE"
    r"|INTO\s+OUTFILE|INTO\s+DUMPFILE"
    r"|SYS\.|MYSQL\.|PG_SLEEP|WAITFOR|BENCHMARK|SLEEP)\b",
    re.IGNORECASE,
)
```

This catches obvious attacks early (e.g., `DROP TABLE users`, `SELECT SLEEP(10)`) without the overhead of full AST parsing.

**Stage 2: AST Parsing via `sqlglot`**

The query is parsed into an Abstract Syntax Tree using the `sqlglot` library. The validator checks:

1. The parse succeeds (rejects malformed SQL).
2. Exactly one statement is present (rejects stacked queries like `SELECT 1; DROP TABLE users`).
3. The top-level statement is a `SELECT` (rejects `INSERT`, `UPDATE`, `DELETE`, `CREATE`, etc.).

**Stage 3: Subquery Scanning**

The validator walks the entire AST tree recursively, checking that no subquery contains a write operation:

```python
def _check_subqueries(self, stmt):
    for node in stmt.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop,
                              exp.Create, exp.Command)):
            raise QuerySecurityError(...)
```

This prevents injection patterns like:
```sql
SELECT * FROM users WHERE id IN (DELETE FROM users RETURNING id)
```

#### b) API Key Validation

The `validate_api_key()` function uses `hmac.compare_digest()` for constant-time string comparison, preventing timing attacks that could leak the API key character-by-character:

```python
import hmac
return hmac.compare_digest(provided.encode(), expected.encode())
```

#### c) TLS Context Builder

The `build_ssl_context()` function creates an `ssl.SSLContext` configured with TLS 1.2+ for the HTTP transport.

---

### 6.5 Policy Engine

**File**: `core/policy.py`

The Policy Engine enforces YAML-based access control rules at runtime. It operates independently of the Security Engine — the Security Engine validates SQL syntax, while the Policy Engine controls what data the LLM is allowed to see.

#### Policy Resolution

Policies are defined in `policies/policies.yaml` with two levels:

1. **Defaults**: Applied to all connections unless overridden.
2. **Per-connection policies**: Override defaults for a specific database.

```yaml
defaults:
  allow_query_execution: true
  max_rows_per_query: 100

policies:
  my_postgres:
    max_rows_per_query: 50     # Override: tighter limit
    tables:
      deny: [audit_logs]      # Hide this table
    column_masks:
      users: [password_hash]   # Mask this column
```

The `_get_policy()` method resolves the effective `ConnectionPolicy` by merging per-connection settings over defaults.

#### Access Control Operations

| Method | Description | Raises on Violation |
|---|---|---|
| `assert_schema_access(conn)` | Is schema introspection allowed? | `PolicyViolation` |
| `assert_table_access(conn, table)` | Is this table visible? | `PolicyViolation` |
| `assert_query_execution(conn)` | Can the LLM run queries? | `PolicyViolation` |
| `assert_sample_data(conn)` | Can the LLM preview rows? | `PolicyViolation` |

#### Table Filtering

Two modes:

- **Allowlist mode**: If `tables.allow` is defined, ONLY those tables are visible. Everything else is hidden.
- **Blocklist mode**: If `tables.deny` is defined, those tables are hidden. Everything else is visible.

The comparison is case-insensitive (`table_name.lower()`).

#### Column Masking

When column masking is configured for a table (e.g., `users: [password_hash, ssn]`), two things happen:

1. **In schema descriptions**: The column gets a `masked: true` flag. The LLM knows the column exists but cannot read its values.
2. **In query results**: The column's value is replaced with the string `***MASKED***`.

#### Row Limit Enforcement

Every query result is truncated to `max_rows_per_query` (default: 100). Sample data requests use a separate, typically smaller limit (`sample_data_max_rows`, default: 5).

---

### 6.6 Schema Introspector

**File**: `core/schema.py`

The `SchemaIntrospector` is the **central orchestration layer**. It is the only class that the MCP tool dispatcher calls. It composes three components:

- **ConnectorRegistry** — to get database connections
- **PolicyEngine** — to enforce access control
- **SecurityEngine** (via `get_query_validator()`) — to validate SQL

Every method follows the same pattern:

```
1. Assert policy permission
2. Get connector from registry
3. Call connector method
4. Apply policy transformations (filtering, masking, limiting)
5. Return structured dict
```

For example, `execute_query()` does:

```python
async def execute_query(self, connection_name, sql, table_hint=None):
    # 1. Policy: is query execution allowed?
    self._policy.assert_query_execution(connection_name)

    # 2. Get connector
    connector = await self._registry.get(connection_name)

    # 3. Security: validate SQL (AST parsing)
    validator = get_query_validator()
    clean_sql = validator.validate(sql, dialect=connector.get_dialect())

    # 4. Execute with row limit
    row_limit = self._policy.get_row_limit(connection_name)
    rows = await connector.execute_query(clean_sql, limit=row_limit)
    rows = self._policy.enforce_row_limit(connection_name, rows)

    # 5. Apply column masks
    if table_hint:
        rows = self._policy.apply_column_masks(connection_name, table_hint, rows)

    return {"connection": connection_name, "sql": clean_sql, "rows": rows, ...}
```

---

### 6.7 MCP Server

**File**: `server.py`

The server module has three responsibilities:

**a) Server Factory (`create_server()`)**

Initializes the global state (registry, policy engine, introspector) and creates the MCP `Server` instance with tool handlers registered:

```python
async def create_server() -> Server:
    _policy = PolicyEngine()
    _registry = ConnectorRegistry()
    await _registry.initialize(eager=False)
    _introspector = SchemaIntrospector(_registry, _policy)

    server = Server(name="mcp-db-wrapper", version="0.1.0")
    server.list_tools()   → _get_tool_definitions()
    server.call_tool()    → _dispatch_tool()
    return server
```

**b) Tool Definitions (`_get_tool_definitions()`)**

Returns a list of `Tool` objects with JSON Schema `inputSchema` definitions. These are advertised to the LLM so it knows what arguments each tool accepts.

**c) Tool Dispatcher (`_dispatch_tool()`)**

A `match` statement routes each tool call to the appropriate `SchemaIntrospector` or `ConnectorRegistry` method. All exceptions are caught and serialized as error responses:

```python
async def _dispatch_tool(name, args):
    try:
        match name:
            case "list_tables":
                result = await introspector.list_tables(args["connection"])
                return _ok(result)
            case "execute_query":
                result = await introspector.execute_query(...)
                return _ok(result)
            # ... 9 more tools
    except PolicyViolation as e:
        return _err(f"Policy violation: {e}")
    except Exception as e:
        return _err(f"Error: {e}")
```

---

### 6.8 Transport Layer

**Directory**: `transport/`

#### stdio Transport (`stdio_transport.py`)

The simplest transport. The MCP server reads JSON-RPC messages from `stdin` and writes responses to `stdout`:

```python
async def run_stdio():
    server = await create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, ...)
```

This is the standard mode for local AI clients like Cursor IDE and Claude Desktop, which spawn the server as a child process and communicate over pipes.

#### HTTP/SSE Transport (`http_transport.py`)

For remote clients, the server exposes a FastAPI application with four endpoints:

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/` | GET | No | Server info (name, version, endpoints) |
| `/health` | GET | Yes | Connection health status |
| `/sse` | GET | Yes | Server-Sent Events stream for MCP communication |
| `/messages` | POST | Yes | MCP message endpoint (paired with SSE) |

The FastAPI app uses a **lifespan context manager** to initialize the MCP server on startup and shut it down gracefully:

```python
@asynccontextmanager
async def lifespan(app):
    app.state.mcp_server = await create_server()
    yield
    await shutdown_server()
```

Authentication is handled by a dependency that extracts and validates a Bearer token from the `Authorization` header.

---

### 6.9 CLI

**File**: `cli.py`

Built with `Typer` and `Rich` for beautiful terminal output. Five subcommands:

| Command | Description |
|---|---|
| `mcp-db-wrapper serve` | Start the server (with `--transport`, `--host`, `--port` flags) |
| `mcp-db-wrapper test-conn <name>` | Test a single database connection |
| `mcp-db-wrapper list-conn` | List all configured connections in a table |
| `mcp-db-wrapper validate` | Validate YAML configs without starting |
| `mcp-db-wrapper version` | Print version |

The `serve` command supports three transport modes: `stdio`, `http`, or `both` (runs both concurrently via `asyncio.gather`).

---

## 7. Request Lifecycle — End to End

Here is the complete journey of a single `execute_query` tool call, from the LLM to the database and back:

```
LLM sends:
  {"method": "tools/call", "params": {"name": "execute_query",
   "arguments": {"connection": "prod_db", "sql": "SELECT name, email FROM users WHERE active = true"}}}

Step 1: TRANSPORT receives the JSON-RPC message
  → stdio: read from stdin
  → HTTP: POST /messages

Step 2: MCP SERVER dispatches to _dispatch_tool("execute_query", {...})

Step 3: SCHEMA INTROSPECTOR.execute_query("prod_db", "SELECT ...")
  │
  ├─ Step 3a: POLICY ENGINE.assert_query_execution("prod_db")
  │    → Checks: allow_query_execution == true?    ✓
  │
  ├─ Step 3b: CONNECTOR REGISTRY.get("prod_db")
  │    → First call? Instantiate PostgresConnector, call connect()
  │    → Subsequent calls? Return cached connector
  │
  ├─ Step 3c: SECURITY ENGINE.validate(sql, dialect="postgres")
  │    → Regex pre-filter: no dangerous keywords?     ✓
  │    → AST parse: one SELECT statement?              ✓
  │    → Subquery walk: no write ops in subqueries?    ✓
  │    → Returns cleaned SQL string
  │
  ├─ Step 3d: CONNECTOR.execute_query(clean_sql, limit=50)
  │    → asyncpg executes the query with a LIMIT cap
  │    → Returns list of row dicts
  │
  ├─ Step 3e: POLICY ENGINE.enforce_row_limit("prod_db", rows)
  │    → Truncates to max_rows_per_query (50)
  │
  └─ Step 3f: POLICY ENGINE.apply_column_masks("prod_db", "users", rows)
       → "email" is masked → replaced with "***MASKED***"

Step 4: MCP SERVER serializes result to JSON TextContent

Step 5: TRANSPORT sends the response
  → stdio: write to stdout
  → HTTP: stream via SSE

LLM receives:
  {"rows": [{"name": "Alice", "email": "***MASKED***"}, ...], "row_count": 50}
```

---

## 8. Security Architecture

Security is enforced at **four independent layers**, any one of which can block a request:

```
Request from LLM
       │
       ▼
┌─────────────────────────────────────────┐
│  Layer 1: TRANSPORT AUTH                │
│  HTTP: Bearer token validation          │
│  (hmac.compare_digest — timing-safe)    │
│  stdio: no auth (trusted local process) │
└─────────────────┬───────────────────────┘
                  │ ✓ authenticated
       ▼
┌─────────────────────────────────────────┐
│  Layer 2: POLICY ENGINE                 │
│  ✓ Schema introspection allowed?        │
│  ✓ Table in allowlist / not blocklisted?│
│  ✓ Query execution enabled?             │
│  ✓ Sample data allowed?                 │
└─────────────────┬───────────────────────┘
                  │ ✓ authorized
       ▼
┌─────────────────────────────────────────┐
│  Layer 3: SQL SECURITY VALIDATOR        │
│  ✓ Regex: no DROP/DELETE/INSERT/...     │
│  ✓ AST: exactly one SELECT statement    │
│  ✓ Walk: no write ops in subqueries     │
└─────────────────┬───────────────────────┘
                  │ ✓ safe SQL
       ▼
┌─────────────────────────────────────────┐
│  Layer 4: CONNECTOR GUARDRAILS          │
│  ✓ LIMIT injected into SQL              │
│  ✓ Row results truncated by policy      │
│  ✓ Sensitive columns masked             │
└─────────────────────────────────────────┘
```

**Defense-in-depth**: Even if one layer is bypassed (e.g., a novel SQL injection technique evades the regex), the AST parser will still reject non-SELECT statements. Even if the AST is somehow fooled, the connector injects a `LIMIT` clause, and the policy engine masks sensitive columns in the result.

---

## 9. Configuration Reference

### 9.1 Environment Variables

All environment variables use the `MCP_` prefix.

| Variable | Default | Description |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | HTTP server bind address |
| `MCP_PORT` | `8000` | HTTP server port |
| `MCP_TRANSPORT` | `both` | Transport mode: `stdio`, `http`, or `both` |
| `MCP_LOG_LEVEL` | `INFO` | Logging level |
| `MCP_API_KEY` | _(none)_ | Bearer token for HTTP auth. If unset, HTTP is unauthenticated. |
| `MCP_ENABLE_TLS` | `false` | Enable TLS for HTTP transport |
| `MCP_TLS_CERT_PATH` | _(none)_ | Path to TLS certificate file |
| `MCP_TLS_KEY_PATH` | _(none)_ | Path to TLS private key file |
| `MCP_POLICY_PATH` | `policies/policies.yaml` | Path to policy config |
| `MCP_CONNECTIONS_PATH` | `config/connections.yaml` | Path to connection config |

Database-specific variables (used in YAML via `${VAR}` interpolation):

| Variable | Example |
|---|---|
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | `localhost`, `5432`, `myuser`, `secret`, `mydb` |
| `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DB` | Similar |
| `MONGODB_URL`, `MONGODB_DB` | `mongodb://localhost:27017`, `mydb` |
| `SQLITE_PATH` | `./data/mydb.sqlite` |
| `REDIS_URL` | `redis://localhost:6379/0` |
| `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_DB_URL` | Supabase project URL, anon key, direct PG URL |

### 9.2 Connection Configuration (YAML)

File: `config/connections.yaml`

```yaml
connections:
  # Each key is a connection name referenced by MCP tools
  my_postgres:
    type: postgres            # Required: database type
    host: "${POSTGRES_HOST}"  # Supports ${ENV_VAR} interpolation
    port: 5432
    database: "${POSTGRES_DB}"
    user: "${POSTGRES_USER}"
    password: "${POSTGRES_PASSWORD}"
    ssl: false
    pool_min: 1               # Connector-specific options
    pool_max: 10
    description: "Main production database"

  my_sqlite:
    type: sqlite
    path: "${SQLITE_PATH}"
    description: "Local dev database"

  my_mongo:
    type: mongodb
    url: "${MONGODB_URL}"
    database: "${MONGODB_DB}"

  my_supabase:
    type: supabase
    url: "${SUPABASE_URL}"
    key: "${SUPABASE_KEY}"
    db_url: "${SUPABASE_DB_URL}"   # Direct PostgreSQL URL (required)
```

Supported `type` values: `postgres`, `postgresql`, `mysql`, `mariadb`, `mongodb`, `mongo`, `sqlite`, `redis`, `mssql`, `sqlserver`, `supabase`.

### 9.3 Policy Configuration (YAML)

File: `policies/policies.yaml`

```yaml
# Global defaults (applied to all connections)
defaults:
  allow_schema_introspection: true
  allow_query_execution: true
  max_rows_per_query: 100
  allow_sample_data: true
  sample_data_max_rows: 5

# Per-connection overrides
policies:
  my_postgres:
    description: "Production DB — restricted access"
    max_rows_per_query: 50

    # Table access control (choose ONE mode)
    tables:
      # Allowlist: ONLY these tables are visible
      allow:
        - users
        - products
        - orders
      # OR blocklist: these tables are HIDDEN
      # deny:
      #   - audit_logs
      #   - admin_configs

    # Column masking: values replaced with ***MASKED***
    column_masks:
      users:
        - password_hash
        - ssn
        - credit_card

    allow_sample_data: true
    sample_data_max_rows: 3

  sensitive_db:
    # Lock down completely
    allow_schema_introspection: false
    allow_query_execution: false
    allow_sample_data: false
```

---

## 10. Supported Databases

| Database | Connector Class | Async Driver | Schema Source | Query Syntax |
|---|---|---|---|---|
| **PostgreSQL** | `PostgresConnector` | `asyncpg` | `information_schema`, `pg_class` | Standard SQL with `LIMIT` |
| **MySQL / MariaDB** | `MySQLConnector` | `aiomysql` | `INFORMATION_SCHEMA` | Standard SQL with `LIMIT` |
| **SQLite** | `SQLiteConnector` | `aiosqlite` | `PRAGMA` introspection | Standard SQL with `LIMIT` |
| **MongoDB** | `MongoDBConnector` | `motor` | Document sampling (20 docs) | `find()` via `execute_mongo_query` |
| **Redis** | `RedisConnector` | `redis-py` (async) | Key namespace scanning | Read-only commands only |
| **MSSQL** | `MSSQLConnector` | `aioodbc` | `INFORMATION_SCHEMA` | T-SQL with `SELECT TOP N` |
| **Supabase** | `SupabaseConnector` | `asyncpg` + `supabase-py` | `information_schema` (via direct PG URL) | Standard SQL with `LIMIT` |

### Database-Specific Behaviors

**MongoDB**: Since MongoDB is schema-less, the connector samples 20 documents per collection and infers field types (e.g., `string`, `integer`, `ObjectId`). The `execute_query()` method raises `NotImplementedError` — MongoDB queries must use the dedicated `execute_mongo_query` tool, which accepts a filter dict and projection.

**Redis**: Redis has no tables or schemas in the relational sense. The connector scans keys using `SCAN` and groups them by namespace prefix (the part before the first `:`). For example, keys `user:1`, `user:2`, `user:3` create a namespace called `user`. Hash fields within that namespace become "columns." Only read-only Redis commands are permitted (GET, HGETALL, LRANGE, etc.).

**Supabase**: Inherits from `PostgresConnector` and connects directly to Supabase's underlying PostgreSQL via a `db_url`. The Supabase client SDK is optionally initialized for metadata but is not used for schema introspection or queries.

---

## 11. Deployment

### 11.1 Local Development

```bash
# Clone and install
git clone <repo>
cd mcp-db-wrapper
pip install -e ".[dev]"

# Configure
cp .env.example .env
cp config/connections.example.yaml config/connections.yaml
cp policies/policies.example.yaml policies/policies.yaml
# Edit .env with your database credentials

# Validate
mcp-db-wrapper validate

# Start (stdio for local IDEs)
mcp-db-wrapper serve --transport stdio

# Start (HTTP for remote clients)
mcp-db-wrapper serve --transport http --port 8000
```

### 11.2 Docker

**Dockerfile**: Multi-stage build based on `python:3.11-slim` with system dependencies for ODBC, PostgreSQL, and MySQL drivers. Includes a health check endpoint.

```bash
# Build and run
docker compose up -d

# Check health
curl http://localhost:8000/health
```

The `docker-compose.yml` mounts `config/` and `policies/` as read-only volumes, so you can update configurations without rebuilding the image.

### 11.3 Integrating with AI Clients

**Cursor IDE / VS Code** (stdio):
```json
{
  "mcpServers": {
    "db-wrapper": {
      "command": "mcp-db-wrapper",
      "args": ["serve", "--transport", "stdio"],
      "env": {
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PASSWORD": "secret"
      }
    }
  }
}
```

**Claude Desktop** (stdio):
```json
{
  "mcpServers": {
    "db-wrapper": {
      "command": "mcp-db-wrapper",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

**Remote HTTP clients** (ChatGPT plugins, web apps):
```json
{
  "mcpServers": {
    "db-wrapper": {
      "url": "http://your-server:8000/sse",
      "headers": {
        "Authorization": "Bearer your-api-key"
      }
    }
  }
}
```

---

## 12. Testing

The project ships with **99 tests** across four test files:

### Unit Tests

| File | Tests | Coverage |
|---|---|---|
| `test_policy.py` | 14 | PolicyEngine: allowlists, blocklists, column masking, row limits, sample limits, policy summaries |
| `test_security.py` | 11 | QueryValidator: SELECT allowed, JOIN/subquery/aggregate allowed, INSERT/UPDATE/DELETE/DROP/CREATE/EXEC/SLEEP rejected, stacked queries rejected |
| `test_sqlite_connector.py` | 8 | SQLiteConnector: list tables, describe table, relationships, query execution, sample data, schema map, DB stats |

### End-to-End Integration Tests

| File | Tests | Coverage |
|---|---|---|
| `test_e2e_real_db.py` | 60 | Full-stack testing against a 12-table SQLite database with hundreds of rows |

The E2E test suite creates a comprehensive e-commerce schema:

```
12 tables + 2 views, ~200 rows of realistic data:

users(10) → addresses(15), orders(15), reviews(20)
categories(8) → products(20) → product_images(20), product_tags(40)
orders(15) → order_items(25), payments(15)
coupons(5), audit_log(5)
Views: active_products_view, order_summary_view
```

The E2E tests are organized into eight test classes:

| Class | What It Tests |
|---|---|
| `TestDatabaseStructure` | Schema creation, table counts, column metadata, view definitions |
| `TestConnectorQueries` | JOINs, aggregates, subqueries, multi-table joins, views, LIMIT enforcement |
| `TestPolicyWithRealData` | Table blocking, column masking, row limits, schema mask flags, full lockdown |
| `TestSchemaIntrospectorE2E` | Full-stack: policy + connector + masking integration |
| `TestSQLSecurityRealWorld` | UNION injection, stacked queries, comment bypass, real-world attack patterns |
| `TestConnectorRegistry` | Connector lifecycle, lazy connection, health checks, shutdown |
| `TestDataIntegrity` | Foreign key consistency, order total math, referential integrity |
| `TestEdgeCases` | Empty results, long queries, case sensitivity, self-referencing FKs, concurrent queries |

**Running tests**:
```bash
python -m pytest tests/ -v
```

---

## 13. Design Decisions & Trade-offs

### Why `sqlglot` instead of simple regex?

Regex-only SQL validation is trivially bypassable. An attacker can use string concatenation, Unicode tricks, or nested subqueries to smuggle write operations past a regex filter. `sqlglot` parses SQL into a proper AST, which allows the validator to structurally verify that the statement is a SELECT and that no subquery contains a write operation. The regex layer is kept as a fast pre-filter for obvious attacks.

### Why module-level singletons in `server.py`?

The MCP protocol expects a single server instance per process. Module-level globals (`_registry`, `_policy`, `_introspector`) are the simplest way to share state between the server factory and the tool dispatcher. This limits the system to one server per process, which is sufficient for the stdio use case (one process per IDE) and the HTTP use case (one process per container). A documented comment warns about this limitation.

### Why lazy loading in `core/__init__.py`?

The project has a circular import chain: `connectors/__init__.py` → `postgres.py` → `core.config` → `core/__init__.py` → `core.registry` → `connectors/__init__.py`. Breaking this cycle with lazy loading (`__getattr__`) in `core/__init__.py` allows all modules to be imported without import-order dependencies.

### Why async everywhere?

Database I/O is inherently I/O-bound. Using async drivers (`asyncpg`, `aiomysql`, `aiosqlite`, `motor`) allows the server to handle multiple concurrent tool calls without blocking. This is critical for the HTTP transport, where multiple LLM clients may be querying simultaneously.

### Why not an ORM?

The connector abstraction is deliberately thin — it runs raw SQL queries and returns plain dicts. An ORM would add unnecessary complexity, introduce its own query language, and make it harder to validate the exact SQL being sent to the database.

---

## 14. Glossary

| Term | Definition |
|---|---|
| **MCP** | Model Context Protocol — an open standard for LLM-to-tool communication. |
| **Tool** | A named operation that an LLM can invoke (e.g., `list_tables`, `execute_query`). |
| **Transport** | The communication channel between the LLM client and the MCP server (stdio or HTTP/SSE). |
| **Connector** | A database driver implementation that provides schema introspection and query execution. |
| **Registry** | The lifecycle manager for all connector instances (lazy connect, caching, shutdown). |
| **Policy** | A set of YAML-defined rules controlling what tables, columns, and operations are accessible. |
| **Column Mask** | A policy rule that replaces sensitive column values with `***MASKED***` in query results. |
| **Allowlist** | A list of tables that the LLM is explicitly permitted to see. All others are hidden. |
| **Blocklist** | A list of tables that the LLM is explicitly forbidden from seeing. All others are visible. |
| **AST** | Abstract Syntax Tree — a structured representation of a SQL query produced by `sqlglot`. |
| **SSE** | Server-Sent Events — a unidirectional HTTP streaming protocol used by the HTTP transport. |
| **JSON-RPC** | A lightweight remote procedure call protocol using JSON. The wire format for MCP. |
| **`sqlglot`** | A Python library for SQL parsing, transpilation, and optimization. Used for security validation. |

---

*Document generated for MCP DB Wrapper v0.1.0. Last updated: August 2026.*
