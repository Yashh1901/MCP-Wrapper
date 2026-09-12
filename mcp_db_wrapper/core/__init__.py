"""
core/__init__.py — Core subpackage exports

Uses lazy imports to avoid circular dependency:
  connectors/__init__.py → core.config → core/__init__.py → core.registry → connectors (cycle!)
"""


def __getattr__(name: str):
    """Lazy-load submodule exports to break circular imports."""
    _IMPORTS = {
        "Settings": ("mcp_db_wrapper.core.config", "Settings"),
        "load_settings": ("mcp_db_wrapper.core.config", "load_settings"),
        "ConnectorRegistry": ("mcp_db_wrapper.core.registry", "ConnectorRegistry"),
        "PolicyEngine": ("mcp_db_wrapper.core.policy", "PolicyEngine"),
        "SchemaIntrospector": ("mcp_db_wrapper.core.schema", "SchemaIntrospector"),
    }
    if name in _IMPORTS:
        module_path, attr = _IMPORTS[name]
        import importlib

        mod = importlib.import_module(module_path)
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ConnectorRegistry",
    "PolicyEngine",
    "SchemaIntrospector",
    "Settings",
    "load_settings",
]
