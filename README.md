# 🔌 MCP DB Wrapper

> **Universal MCP Database Wrapper** — Connect any database to any LLM/GenAI tool securely.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://python.org)
[![MCP](https://img.shields.io/badge/Protocol-MCP-purple)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/License-MIT-green)](#license)

---

## What is MCP DB Wrapper?

MCP DB Wrapper is a Python library and server that acts as a **secure bridge between databases and AI tools** via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io).

It gives LLMs like ChatGPT, Gemini, Claude, GitHub Copilot, Cursor IDE, and Antigravity instant, structured knowledge about your databases — tables, columns, schemas, relationships, data types, and sample data — while keeping **you in full control** via YAML-based security policies.

```
  ┌─────────────────────────────────────────────────────────────────┐
  │   AI Tools (ChatGPT, Claude, Gemini, Cursor, Copilot, AGY...)   │
  └──────────────────────────┬──────────────────────────────────────┘
                             │  MCP Protocol (stdio / HTTP+SSE)
  ┌──────────────────────────▼──────────────────────────────────────┐
  │                  MCP DB Wrapper Server                          │
  │                                                                 │
  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
  │  │ Policy      │  │ Security     │  │ Schema Introspector  │   │
  │  │ Engine      │  │ (SQL AST     │  │ (tables, columns,    │   │
  │  │ (YAML rules)│  │  validator)  │  │  FK relationships)   │   │
  │  └─────────────┘  └──────────────┘  └──────────────────────┘   │
  └────────────────────────────┬────────────────────────────────────┘
            ┌──────────────────┼──────────────────────┐
            ▼                  ▼                       ▼
     ┌────────────┐    ┌─────────────┐       ┌──────────────┐
     │ PostgreSQL │    │   MongoDB   │       │  MySQL/MSSQL │
     │  Supabase  │    │    Redis    │       │    SQLite    │
     └────────────┘    └─────────────┘       └──────────────┘
```

---

## Features

| Feature | Description |
|---|---|
| 🗄️ **Multi-DB Support** | PostgreSQL, MySQL, MongoDB, SQLite, Redis, MSSQL, Supabase |
| 🔌 **Dual Transport** | stdio (local tools) + HTTP/SSE (remote/cloud tools) |
| 🧠 **Schema Intelligence** | Tables, columns, types, PKs, FKs, indexes, row counts |
| 🔒 **Policy Engine** | YAML-based table allowlists/blocklists, column masking |
| 🛡️ **SQL Security** | AST-level validation — only SELECT allowed, injection-proof |
| 🔑 **Auth** | Bearer token API key for HTTP transport |
| 🔐 **TLS** | Optional TLS for encrypted connections |
| 📊 **DB Stats** | Row counts, database sizes, index info |
| 🎯 **Env Var Substitution** | Secrets stay in `.env`, not in YAML |
| ⚡ **Async** | Fully async with connection pooling |

---

## Supported Databases

| Database | Driver | Schema Introspection | Query Execution |
|---|---|---|---|
| **PostgreSQL** | asyncpg | ✅ Full (information_schema) | ✅ SELECT |
| **MySQL / MariaDB** | aiomysql | ✅ Full (INFORMATION_SCHEMA) | ✅ SELECT |
| **MongoDB** | motor | ✅ Inferred from sampling | ✅ find() |
| **SQLite** | aiosqlite | ✅ Full (PRAGMA) | ✅ SELECT |
| **Redis** | redis-py | ✅ Key namespace analysis | ✅ Read-only cmds |
| **MSSQL / SQL Server** | aioodbc | ✅ Full (INFORMATION_SCHEMA) | ✅ SELECT |
| **Supabase** | asyncpg + supabase-py | ✅ Full (via PostgreSQL) | ✅ SELECT |

---

## Quick Start

### 1. Install

```bash
pip install mcp-db-wrapper
```

Or for development:
```bash
git clone <repo>
cd mcp-db-wrapper
pip install -e ".[dev]"
```

### 2. Configure

Copy the example files:
```bash
cp .env.example .env
cp config/connections.yaml my-connections.yaml
cp policies/policies.yaml my-policies.yaml
```

Edit `.env` with your database credentials:
```env
POSTGRES_HOST=localhost
POSTGRES_DB=mydb
POSTGRES_USER=myuser
POSTGRES_PASSWORD=mypassword

MCP_API_KEY=my-secret-key-for-http
```

Edit `config/connections.yaml`:
```yaml
connections:
  my_db:
    type: postgres
    host: "${POSTGRES_HOST}"
    port: 5432
    database: "${POSTGRES_DB}"
    user: "${POSTGRES_USER}"
    password: "${POSTGRES_PASSWORD}"
    description: "My production database"
```

Edit `policies/policies.yaml`:
```yaml
policies:
  my_db:
    allow_query_execution: true
    max_rows_per_query: 50
    tables:
      deny:
        - user_tokens
        - audit_logs
    column_masks:
      users:
        - password_hash
        - credit_card
```

### 3. Validate

```bash
mcp-db-wrapper validate
```

### 4. Start

**Local mode (stdio — for Cursor IDE, Claude Desktop, Antigravity):**
```bash
mcp-db-wrapper serve --transport stdio
```

**Remote mode (HTTP/SSE — for web-based AI tools):**
```bash
mcp-db-wrapper serve --transport http --port 8000
```

**Both simultaneously:**
```bash
mcp-db-wrapper serve --transport both
```

---

## Integrating with AI Tools

### Cursor IDE / VS Code

Add to your `.cursor/mcp.json` or `mcp.json`:
```json
{
  "mcpServers": {
    "db-wrapper": {
      "command": "mcp-db-wrapper",
      "args": ["serve", "--transport", "stdio"],
      "env": {
        "POSTGRES_HOST": "localhost",
        "POSTGRES_DB": "mydb",
        "POSTGRES_USER": "user",
        "POSTGRES_PASSWORD": "password"
      }
    }
  }
}
```

### Claude Desktop

Add to `claude_desktop_config.json`:
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

### Antigravity / Other HTTP MCP Clients

```json
{
  "mcpServers": {
    "db-wrapper": {
      "url": "http://localhost:8000/sse",
      "headers": {
        "Authorization": "Bearer your-api-key"
      }
    }
  }
}
```

---

## Available MCP Tools

Once connected, the following tools are available to LLMs:

| Tool | Description |
|---|---|
| `list_connections` | List all configured databases |
| `list_tables` | List tables/collections in a database |
| `describe_table` | Full column schema for a table |
| `get_schema_map` | Complete database schema in one call |
| `get_relationships` | FK relationships between tables |
| `execute_query` | Run a SELECT query (SQL databases) |
| `execute_mongo_query` | Run a find() query (MongoDB) |
| `get_sample_data` | Get N sample rows from a table |
| `get_db_stats` | Row counts, sizes, version info |
| `get_policy_summary` | Show active policy for a connection |
| `health_check` | Check all connection health |

---

## Policy Configuration

Policies are defined in `policies/policies.yaml`. They give you granular control over what the AI can see and do.

### Table Allowlist (whitelist)
Only these tables are visible:
```yaml
policies:
  my_db:
    tables:
      allow:
        - users
        - products
        - orders
```

### Table Blocklist (blacklist)
Hide specific tables:
```yaml
policies:
  my_db:
    tables:
      deny:
        - audit_logs
        - admin_configs
        - payment_tokens
```

### Column Masking
Mask sensitive values (replaced with `***MASKED***`):
```yaml
policies:
  my_db:
    column_masks:
      users:
        - password_hash
        - ssn
        - credit_card_number
        - phone
```

### Query & Row Limits
```yaml
policies:
  my_db:
    allow_query_execution: true
    max_rows_per_query: 50       # max rows per SELECT
    allow_sample_data: true
    sample_data_max_rows: 3      # max rows for sample data
```

### Lock Down Completely
```yaml
policies:
  sensitive_db:
    allow_schema_introspection: false
    allow_query_execution: false
    allow_sample_data: false
```

---

## Security Architecture

```
Request from LLM
    │
    ▼
┌─────────────────────────────────┐
│  Transport Auth                 │
│  (Bearer token for HTTP/SSE)    │
└────────────────┬────────────────┘
                 │
    ▼
┌─────────────────────────────────┐
│  Policy Engine                  │
│  ✓ Schema access allowed?       │
│  ✓ Table in allowlist?          │
│  ✓ Table not blocklisted?       │
│  ✓ Query execution allowed?     │
└────────────────┬────────────────┘
                 │
    ▼
┌─────────────────────────────────┐
│  SQL Security Validator         │
│  ✓ Parses SQL to AST            │
│  ✓ Only SELECT statements       │
│  ✓ No dangerous keywords        │
│  ✓ No multiple statements       │
│  ✓ No write ops in subqueries   │
└────────────────┬────────────────┘
                 │
    ▼
┌─────────────────────────────────┐
│  Database Connector             │
│  ✓ LIMIT injected               │
│  ✓ Parameterized queries        │
│  ✓ Connection pool              │
└────────────────┬────────────────┘
                 │
    ▼
┌─────────────────────────────────┐
│  Response Transformation        │
│  ✓ Column masking applied       │
│  ✓ Row limit enforced           │
│  ✓ JSON serialization           │
└─────────────────────────────────┘
```

---

## CLI Reference

```bash
# Start server
mcp-db-wrapper serve [--transport stdio|http|both] [--host HOST] [--port PORT]

# Test a connection
mcp-db-wrapper test-conn my_postgres

# List configured connections
mcp-db-wrapper list-conn

# Validate config files
mcp-db-wrapper validate

# Show version
mcp-db-wrapper version
```

---

## Running Tests

```bash
# All tests (uses SQLite, no external DB needed for core tests)
pytest tests/ -v

# With coverage
pytest tests/ -v --cov=mcp_db_wrapper --cov-report=html
```

---

## Project Structure

```
mcp_db_wrapper/
├── __init__.py          # Package version
├── __main__.py          # python -m mcp_db_wrapper
├── server.py            # MCP server + all tool definitions
├── cli.py               # typer CLI
├── core/
│   ├── config.py        # Settings + YAML loader with env interpolation
│   ├── policy.py        # Policy engine (YAML-based access control)
│   ├── registry.py      # Connector lifecycle manager
│   ├── schema.py        # High-level schema introspection (policy-aware)
│   └── security.py      # SQL validator + API key auth + TLS
├── connectors/
│   ├── base.py          # Abstract connector interface
│   ├── postgres.py      # PostgreSQL (asyncpg)
│   ├── mysql.py         # MySQL (aiomysql)
│   ├── mongodb.py       # MongoDB (motor)
│   ├── sqlite.py        # SQLite (aiosqlite)
│   ├── redis.py         # Redis (redis-py async)
│   ├── mssql.py         # MSSQL (aioodbc)
│   └── supabase.py      # Supabase (asyncpg + supabase-py)
└── transport/
    ├── stdio_transport.py  # stdio MCP transport (local tools)
    └── http_transport.py   # FastAPI HTTP/SSE transport (remote tools)
```

---

## License

MIT License — see [LICENSE](LICENSE) for details.
