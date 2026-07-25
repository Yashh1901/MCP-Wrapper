"""
mcp_db_wrapper — Universal MCP Database Wrapper
"""
from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("mcp-db-wrapper")
except PackageNotFoundError:
    __version__ = "0.1.0-dev"

__all__ = ["__version__"]
