"""
cli.py — Command Line Interface

Provides the `mcp-db-wrapper` CLI with subcommands:
  mcp-db-wrapper serve      → Start the MCP server
  mcp-db-wrapper test-conn  → Test a specific database connection
  mcp-db-wrapper list-conn  → List configured connections
  mcp-db-wrapper validate   → Validate configs without starting
  mcp-db-wrapper version    → Show version info
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from mcp_db_wrapper import __version__

app = typer.Typer(
    name="mcp-db-wrapper",
    help="Universal MCP Database Wrapper - connect any DB to any AI tool",
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()


def _safe_policy_template(connection: str, tables: list[str]) -> dict:
    """Create a review-required policy; no query access is granted by default."""
    return {
        "defaults": {
            "allow_schema_introspection": False,
            "allow_query_execution": False,
            "allow_sample_data": False,
            "max_rows_per_query": 100,
            "sample_data_max_rows": 5,
        },
        "policies": {
            connection: {
                "description": "Generated policy. Review table names and enable access deliberately.",
                "allow_schema_introspection": True,
                "allow_query_execution": False,
                "allow_sample_data": False,
                # Preserve the discovered names while preventing accidental
                # broad access if someone enables query execution prematurely.
                "tables": {"deny": tables},
                "column_masks": {},
            }
        },
    }


def _write_yaml(path: Path, data: dict, force: bool) -> None:
    if path.exists() and not force:
        raise typer.BadParameter(f"{path} already exists; use --force to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@app.command("init", help="Interactively create a safe connection and policy configuration.")
def cmd_init(
    force: bool = typer.Option(False, "--force", help="Replace existing generated files."),
) -> None:
    """Bootstrap one local configuration without requiring source-code edits."""
    name = typer.prompt("Connection name", default="my_database").strip()
    db_type = (
        typer.prompt(
            "Database type (sqlite, postgres, mysql, mongodb, mssql, redis, supabase)",
            default="sqlite",
        )
        .strip()
        .lower()
    )
    supported = {"sqlite", "postgres", "mysql", "mongodb", "mssql", "redis", "supabase"}
    if not name or db_type not in supported:
        raise typer.BadParameter("Provide a connection name and a supported database type.")

    connection: dict[str, object] = {"type": db_type, "description": f"{name} database"}
    if db_type == "sqlite":
        connection["path"] = typer.prompt("SQLite file path", default="./data/database.sqlite")
    elif db_type in {"mongodb", "redis", "supabase"}:
        connection["url"] = typer.prompt("Connection URL (or ${ENV_VAR})")
        if db_type == "mongodb":
            connection["database"] = typer.prompt("Database name")
        if db_type == "supabase":
            connection["key"] = typer.prompt(
                "Supabase key environment variable", default="${SUPABASE_KEY}"
            )
    else:
        connection["host"] = typer.prompt("Host", default="localhost")
        connection["database"] = typer.prompt("Database name")
        connection["user"] = typer.prompt("User (or ${ENV_VAR})")
        connection["password"] = typer.prompt(
            "Password environment variable", default="${DB_PASSWORD}"
        )

    _write_yaml(Path("config/connections.yaml"), {"connections": {name: connection}}, force)
    _write_yaml(Path("policies/policies.yaml"), _safe_policy_template(name, []), force)
    console.print("[green]OK Created config/connections.yaml and policies/policies.yaml[/]")
    console.print(
        "[yellow]Policy is deny-first. Run policy-generate, review the tables, then enable queries.[/]"
    )


@app.command(
    "policy-generate", help="Inspect a connection and produce a deny-first policy template."
)
def cmd_policy_generate(
    name: str = typer.Argument(help="Connection name from connections.yaml"),
    output: Path = typer.Option("policies/generated-policy.yaml", "--output", "-o"),  # noqa: B008
    force: bool = typer.Option(False, "--force", help="Replace the output file if it exists."),
) -> None:
    """Generate a policy that exposes schema only until a human enables reads."""

    async def _generate() -> list[str]:
        from mcp_db_wrapper.core.registry import ConnectorRegistry

        registry = ConnectorRegistry()
        try:
            connector = await registry.get(name)
            return await connector.list_tables()
        finally:
            await registry.shutdown()

    tables = asyncio.run(_generate())
    _write_yaml(output, _safe_policy_template(name, tables), force)
    console.print(f"[green]OK Wrote {output} with {len(tables)} discovered tables.[/]")
    console.print(
        "[yellow]Review the allowlist, masks, and set allow_query_execution: true when ready.[/]"
    )


# ------------------------------------------------------------------ #
#  serve
# ------------------------------------------------------------------ #


@app.command("serve", help="Start the MCP server (stdio, HTTP, or both).")
def cmd_serve(
    transport: str | None = typer.Option(
        None,
        "--transport",
        "-t",
        help="Transport mode: stdio | http | both (overrides MCP_TRANSPORT env var)",
    ),
    host: str | None = typer.Option(None, "--host", help="HTTP host (overrides MCP_HOST)"),
    port: int | None = typer.Option(None, "--port", "-p", help="HTTP port (overrides MCP_PORT)"),
    eager: bool = typer.Option(False, "--eager", help="Connect all databases eagerly at startup"),
) -> None:
    """Start the MCP server."""
    import logging

    from mcp_db_wrapper.core.config import load_settings

    settings = load_settings()
    effective_transport = transport or settings.transport

    # Setup logging
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))

    console.print(
        Panel.fit(
            f"[bold cyan]MCP DB Wrapper v{__version__}[/]\n"
            f"Transport: [yellow]{effective_transport}[/] | "
            f"Host: [yellow]{host or settings.host}:{port or settings.port}[/]",
            title="Starting MCP Server",
            border_style="cyan",
        )
    )

    async def _run() -> None:

        if effective_transport == "stdio":
            from mcp_db_wrapper.transport.stdio_transport import run_stdio

            await run_stdio()

        elif effective_transport == "http":
            from mcp_db_wrapper.transport.http_transport import run_http

            await run_http(host=host, port=port)

        elif effective_transport == "both":
            from mcp_db_wrapper.transport.http_transport import run_http
            from mcp_db_wrapper.transport.stdio_transport import run_stdio

            # Run both concurrently (stdio blocks, so run HTTP in background)
            console.print("[dim]Running stdio + HTTP transports concurrently...[/]")
            await asyncio.gather(run_stdio(), run_http(host=host, port=port))

        else:
            console.print(f"[red]Unknown transport: {effective_transport}[/]")
            raise typer.Exit(1)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped by user.[/]")


# ------------------------------------------------------------------ #
#  test-conn
# ------------------------------------------------------------------ #


@app.command("test-conn", help="Test a specific database connection.")
def cmd_test_conn(
    name: str = typer.Argument(help="Connection name from connections.yaml"),
) -> None:
    """Test connectivity to a specific database."""

    async def _test() -> None:
        from mcp_db_wrapper.connectors import get_connector_class
        from mcp_db_wrapper.core.config import load_connections

        connections = load_connections()
        if name not in connections:
            console.print(f"[red]Connection '{name}' not found in config.[/]")
            raise typer.Exit(1)

        config = connections[name]
        console.print(f"Testing [cyan]{name}[/] ({config.type})...")

        try:
            cls = get_connector_class(config.type)
            connector = cls(config)
            await connector.connect()
            tables = await connector.list_tables()
            await connector.disconnect()
            console.print(f"[green]OK Connected successfully![/] Found {len(tables)} tables.")
            if tables:
                console.print(
                    f"  Tables: {', '.join(tables[:5])}{'...' if len(tables) > 5 else ''}"
                )
        except Exception as e:  # noqa: BLE001 - CLI boundary reports driver failures
            console.print(f"[red]ERROR Connection failed: {e}[/]")
            raise typer.Exit(1)

    asyncio.run(_test())


# ------------------------------------------------------------------ #
#  list-conn
# ------------------------------------------------------------------ #


@app.command("list-conn", help="List all configured database connections.")
def cmd_list_conn() -> None:
    """List all connections from connections.yaml."""
    from mcp_db_wrapper.core.config import load_connections

    connections = load_connections()
    if not connections:
        console.print("[yellow]No connections configured.[/]")
        return

    table = Table(title="Configured Database Connections", border_style="cyan")
    table.add_column("Name", style="cyan bold")
    table.add_column("Type", style="yellow")
    table.add_column("Description", style="dim")

    for name, cfg in connections.items():
        table.add_row(name, cfg.type, cfg.description or "-")

    console.print(table)


# ------------------------------------------------------------------ #
#  validate
# ------------------------------------------------------------------ #


@app.command("validate", help="Validate configuration files without starting.")
def cmd_validate() -> None:
    """Validate connections.yaml and policies.yaml."""
    from mcp_db_wrapper.core.config import load_connections, load_policies, load_settings

    errors = []
    warnings = []

    # Settings
    try:
        load_settings()
        console.print("[green]OK[/] Settings loaded")
    except Exception as e:  # noqa: BLE001 - validation should report all config errors
        errors.append(f"Settings: {e}")

    # Connections
    try:
        connections = load_connections()
        console.print(f"[green]OK[/] {len(connections)} connections loaded")
        for name, cfg in connections.items():
            if not cfg.type:
                errors.append(f"Connection '{name}' missing 'type'")
    except Exception as e:  # noqa: BLE001 - validation should report all config errors
        errors.append(f"Connections: {e}")

    # Policies
    try:
        policies = load_policies()
        console.print("[green]OK[/] Policies loaded")
        # Check that policy names match connection names
        conn_names = set(connections.keys()) if "connections" in dir() else set()
        policy_names = set(policies.get("policies", {}).keys())
        orphan = policy_names - conn_names
        if orphan:
            warnings.append(f"Policies defined for unknown connections: {orphan}")
    except Exception as e:  # noqa: BLE001 - validation should report all config errors
        errors.append(f"Policies: {e}")

    if warnings:
        for w in warnings:
            console.print(f"[yellow]WARNING {w}[/]")
    if errors:
        for e in errors:
            console.print(f"[red]ERROR {e}[/]")
        raise typer.Exit(1)
    else:
        console.print("[bold green]OK All configurations valid![/]")


# ------------------------------------------------------------------ #
#  version
# ------------------------------------------------------------------ #


@app.command("version", help="Show version information.")
def cmd_version() -> None:
    """Print version and exit."""
    console.print(f"mcp-db-wrapper v[bold cyan]{__version__}[/]")


# ------------------------------------------------------------------ #
#  Entrypoint
# ------------------------------------------------------------------ #


def main() -> None:
    app()


if __name__ == "__main__":
    main()
