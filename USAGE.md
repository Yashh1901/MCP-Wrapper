# MCP DB Wrapper: deployment guide

MCP DB Wrapper is a read-only Model Context Protocol server. It lets an MCP-capable client discover approved database schema and run policy-constrained queries. It is designed as a database exposure boundary, not as a replacement for database permissions: always use a database credential with the least privileges required.

## Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync --extra dev
Copy-Item config/connections.example.yaml config/connections.yaml
Copy-Item policies/policies.example.yaml policies/policies.yaml
Copy-Item .env.example .env
uv run mcp-db-wrapper validate
```

For a no-code bootstrap, run `uv run mcp-db-wrapper init`. It writes one connection and a deny-first policy. Then run `uv run mcp-db-wrapper policy-generate CONNECTION` to discover tables into a review-required policy template.

Keep `connections.yaml`, `policies.yaml`, and `.env` out of source control. Use `${VARIABLE_NAME}` in YAML for credentials, then set those variables in `.env` or the host environment.

## Configure a connection

Minimal SQLite example (`config/connections.yaml`):

```yaml
connections:
  product_db:
    type: sqlite
    path: ./data/product.sqlite
    description: Product catalog
```

The supplied example file contains PostgreSQL, MySQL, MongoDB, MSSQL, Redis, Supabase, and SQLite shapes. Configure only the connections you intend to expose.

## Write a policy

Policy is evaluated per connection. Start with an allowlist and explicitly mask sensitive columns:

```yaml
defaults:
  allow_schema_introspection: false
  allow_query_execution: false
  allow_sample_data: false
  max_rows_per_query: 100
  sample_data_max_rows: 5

policies:
  product_db:
    allow_schema_introspection: true
    allow_query_execution: true
    allow_sample_data: true
    max_rows_per_query: 50
    tables:
      allow: [products, categories]
    column_masks:
      products: [supplier_cost]
```

Missing live policy files are denied by default. Connections without a named policy inherit the `defaults` block, so make those defaults restrictive. A raw SQL query is validated as a single `SELECT`, checked against every referenced table (including joins and subqueries), capped at the policy row limit, and masked using the parsed table set. Do not grant write privileges to the database user.

## Run

For desktop MCP clients, use stdio:

```powershell
uv run mcp-db-wrapper serve --transport stdio
```

For a remote client, set a strong `MCP_API_KEY` in `.env` and run:

```powershell
uv run mcp-db-wrapper serve --transport http --host 127.0.0.1 --port 8000
```

The MCP SSE endpoint is `http://127.0.0.1:8000/sse`; the paired message endpoint is `/messages`. Put the service behind TLS and a reverse proxy for any non-local deployment. Never expose an unauthenticated HTTP instance to a network.

## Client configuration

An MCP client that supports local stdio can use:

```json
{
  "mcpServers": {
    "database": {
      "command": "uv",
      "args": ["run", "--directory", "C:/absolute/path/to/MCP Wrapper", "mcp-db-wrapper", "serve", "--transport", "stdio"]
    }
  }
}
```

Use the client's UI to add the server if it has one; its field names differ by product.

## Operations checklist

1. Run `uv run mcp-db-wrapper validate` after any config or policy change.
2. Run `uv run mcp-db-wrapper test-conn product_db` before enabling a connection.
3. Give each connector a read-only, narrowly scoped database account.
4. Set `MCP_API_KEY` and TLS before remote use; restrict firewall ingress.
5. Review allowlists and masked columns whenever the schema changes.
6. Run `uv run --extra dev python -m pytest -q` before releases.
7. Keep `MCP_REQUEST_TIMEOUT_SECONDS` low enough for your service-level objective, configure `MCP_HTTP_RATE_LIMIT_PER_MINUTE`, and retain the metadata-only audit log for incident review. For multiple replicas, enforce rate limits at the reverse proxy too.
