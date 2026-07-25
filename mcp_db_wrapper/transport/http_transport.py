"""
transport/http_transport.py — HTTP/SSE MCP Transport

Provides a FastAPI-based HTTP server with Server-Sent Events (SSE)
for remote MCP clients (ChatGPT plugins, Gemini, web apps, etc.)

Endpoints:
  GET  /          → Server info
  GET  /health    → Health check
  GET  /sse       → SSE endpoint (MCP over SSE)
  POST /messages  → MCP message endpoint

Security:
  - Bearer token authentication (API key)
  - CORS configurable
  - Optional TLS
"""
from __future__ import annotations

import structlog
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mcp.server.sse import SseServerTransport

from mcp_db_wrapper.core.config import load_settings
from mcp_db_wrapper.core.security import validate_api_key
from mcp_db_wrapper.server import create_server, shutdown_server

logger = structlog.get_logger(__name__)


def build_app() -> FastAPI:
    """Build and return the FastAPI application."""
    settings = load_settings()

    app = FastAPI(
        title="MCP DB Wrapper",
        description="Universal MCP Database Wrapper — connect any DB to any AI tool",
        version=settings.server_version,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — allow all origins by default (tighten in production)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ----------------------------------------------------------------
    #  Auth dependency
    # ----------------------------------------------------------------
    async def _require_api_key(request: Request) -> None:
        """Extract and validate Bearer token from Authorization header."""
        expected = settings.api_key
        if not expected:
            return  # No key configured → open

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]
        else:
            provided = request.headers.get("X-API-Key", "")

        if not validate_api_key(provided, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # ----------------------------------------------------------------
    #  Startup / Shutdown
    # ----------------------------------------------------------------
    _mcp_server = None

    @app.on_event("startup")
    async def _startup() -> None:
        nonlocal _mcp_server
        _mcp_server = await create_server()
        logger.info("http_transport_started", host=settings.host, port=settings.port)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await shutdown_server()
        logger.info("http_transport_stopped")

    # ----------------------------------------------------------------
    #  Routes
    # ----------------------------------------------------------------

    @app.get("/", tags=["Info"])
    async def root() -> dict:
        return {
            "name": "MCP DB Wrapper",
            "version": settings.server_version,
            "transport": "HTTP/SSE",
            "docs": "/docs",
            "sse_endpoint": "/sse",
            "mcp_endpoint": "/messages",
        }

    @app.get("/health", tags=["Info"])
    async def health(_: None = Depends(_require_api_key)) -> JSONResponse:
        from mcp_db_wrapper.core.registry import ConnectorRegistry
        from mcp_db_wrapper.core.config import load_connections
        # Quick health without full check
        connections = load_connections()
        return JSONResponse({"status": "ok", "connections": len(connections)})

    # SSE endpoint — MCP over Server-Sent Events
    sse_transport = SseServerTransport("/messages")

    @app.get("/sse", tags=["MCP"])
    async def sse_endpoint(
        request: Request, _: None = Depends(_require_api_key)
    ):
        """SSE endpoint for MCP protocol communication."""
        assert _mcp_server is not None, "Server not initialized"
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send  # type: ignore[attr-defined]
        ) as streams:
            await _mcp_server.run(
                streams[0],
                streams[1],
                _mcp_server.create_initialization_options(),
            )

    @app.post("/messages", tags=["MCP"])
    async def post_message(
        request: Request, _: None = Depends(_require_api_key)
    ):
        """MCP message POST endpoint (paired with SSE)."""
        return await sse_transport.handle_post_message(
            request.scope, request.receive, request._send  # type: ignore[attr-defined]
        )

    return app


async def run_http(host: str | None = None, port: int | None = None) -> None:
    """
    Run the MCP server in HTTP/SSE mode.

    Args:
        host: Override host (default from settings).
        port: Override port (default from settings).
    """
    settings = load_settings()
    app = build_app()

    ssl_kwargs = {}
    if settings.enable_tls and settings.tls_cert_path and settings.tls_key_path:
        ssl_kwargs["ssl_certfile"] = settings.tls_cert_path
        ssl_kwargs["ssl_keyfile"] = settings.tls_key_path

    config = uvicorn.Config(
        app,
        host=host or settings.host,
        port=port or settings.port,
        log_level=settings.log_level.lower(),
        **ssl_kwargs,
    )
    server = uvicorn.Server(config)
    logger.info(
        "http_transport_launching",
        host=config.host,
        port=config.port,
        tls=settings.enable_tls,
    )
    await server.serve()
