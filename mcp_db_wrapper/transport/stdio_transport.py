"""
transport/stdio_transport.py — stdio MCP transport

Used for local integrations (Cursor IDE, Claude Desktop, Antigravity CLI, etc.)
The MCP server communicates over stdin/stdout with JSON-RPC messages.
"""
from __future__ import annotations

import asyncio

import structlog
from mcp.server.stdio import stdio_server

from mcp_db_wrapper.server import create_server, shutdown_server

logger = structlog.get_logger(__name__)


async def run_stdio() -> None:
    """
    Run the MCP server in stdio mode.

    This is the standard mode for local AI client integrations.
    The process communicates via stdin/stdout using the MCP protocol.
    """
    logger.info("stdio_transport_starting")
    server = await create_server()

    async with stdio_server() as (read_stream, write_stream):
        logger.info("stdio_transport_ready", message="MCP server listening on stdio")
        try:
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
        finally:
            await shutdown_server()
            logger.info("stdio_transport_stopped")
