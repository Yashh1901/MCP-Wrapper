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
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

from mcp_db_wrapper import __version__

app = typer.Typer(
    name="mcp-db-wrapper",
    help="🔌 Universal MCP Database Wrapper — connect any DB to any AI tool",
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()


# ------------------------------------------------------------------ #
#  serve
# ------------------------------------------------------------------ #

@app.command("serve", help="Start the MCP server (stdio, HTTP, or both).")
def cmd_serve(
    transport: Optional[str] = typer.Option(
        None, "--transport", "-t",
        help="Transport mode: stdio | http | both (overrides MCP_TRANSPORT env var)",
    ),
    host: Optional[str] = typer.Option(None, "--host", help="HTTP host (overrides MCP_HOST)"),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="HTTP port (overrides MCP_PORT)"),
    eager: bool = typer.Option(False, "--eager", help="Connect all databases eagerly at startup"),
) -> None:
    """Start the MCP server."""
    from mcp_db_wrapper.core.config import load_settings
    import structlog
    import logging

    settings = load_settings()
    effective_transport = transport or settings.transport

    # Setup logging
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))

    console.print(Panel.fit(
        f"[bold cyan]MCP DB Wrapper v{__version__}[/]\n"
        f"Transport: [yellow]{effective_transport}[/] | "
        f"Host: [yellow]{host or settings.host}:{port or settings.port}[/]",
        title="🔌 Starting MCP Server",
        border_style="cyan",
    ))

    async def _run() -> None:
        from mcp_db_wrapper.core.registry import ConnectorRegistry

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
        from mcp_db_wrapper.core.config import load_connections
        from mcp_db_wrapper.connectors import get_connector_class

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
            console.print(f"[green]✓ Connected successfully![/] Found {len(tables)} tables.")
            if tables:
                console.print(f"  Tables: {', '.join(tables[:5])}{'...' if len(tables) > 5 else ''}")
        except Exception as e:
            console.print(f"[red]✗ Connection failed: {e}[/]")
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
        table.add_row(name, cfg.type, cfg.description or "—")

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
        settings = load_settings()
        console.print("[green]✓[/] Settings loaded")
    except Exception as e:
        errors.append(f"Settings: {e}")

    # Connections
    try:
        connections = load_connections()
        console.print(f"[green]✓[/] {len(connections)} connections loaded")
        for name, cfg in connections.items():
            if not cfg.type:
                errors.append(f"Connection '{name}' missing 'type'")
    except Exception as e:
        errors.append(f"Connections: {e}")

    # Policies
    try:
        policies = load_policies()
        console.print("[green]✓[/] Policies loaded")
        # Check that policy names match connection names
        conn_names = set(connections.keys()) if "connections" in dir() else set()
        policy_names = set(policies.get("policies", {}).keys())
        orphan = policy_names - conn_names
        if orphan:
            warnings.append(f"Policies defined for unknown connections: {orphan}")
    except Exception as e:
        errors.append(f"Policies: {e}")

    if warnings:
        for w in warnings:
            console.print(f"[yellow]⚠ {w}[/]")
    if errors:
        for e in errors:
            console.print(f"[red]✗ {e}[/]")
        raise typer.Exit(1)
    else:
        console.print("[bold green]✓ All configurations valid![/]")


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
