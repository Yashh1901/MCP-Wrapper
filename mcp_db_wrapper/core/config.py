"""
core/config.py — Application settings loaded from env vars and YAML configs.

Uses pydantic-settings for type-safe, validated configuration.
Sensitive values (passwords, keys) are NEVER logged or exposed via MCP tools.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env file early so subsequent os.getenv calls work
load_dotenv()

# ------------------------------------------------------------------ #
#  Helpers
# ------------------------------------------------------------------ #

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate_env(value: Any) -> Any:
    """Recursively replace ${ENV_VAR} placeholders in YAML values."""
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            env_val = os.getenv(m.group(1), "")
            return env_val
        return _ENV_VAR_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


def _load_yaml(path: str | Path) -> dict:
    """Load a YAML file and interpolate env variables. Falls back to .example.yaml if missing."""
    p = Path(path)
    if not p.exists():
        example_p = p.with_name(p.stem + ".example" + p.suffix)
        if example_p.exists():
            p = example_p
        else:
            return {}
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return _interpolate_env(data)


# ------------------------------------------------------------------ #
#  Settings model
# ------------------------------------------------------------------ #

class Settings(BaseSettings):
    """
    All application settings.
    Values are sourced (in priority order) from:
      1. Environment variables
      2. .env file
      3. Default values below
    """

    model_config = SettingsConfigDict(
        env_prefix="MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    transport: str = "both"          # stdio | http | both
    log_level: str = "INFO"
    server_name: str = "mcp-db-wrapper"
    server_version: str = "0.1.0"

    # Security
    api_key: str | None = None        # HTTP transport bearer token
    enable_tls: bool = False
    tls_cert_path: str | None = None
    tls_key_path: str | None = None

    # Config paths
    policy_path: str = "policies/policies.yaml"
    connections_path: str = "config/connections.yaml"

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, v: str) -> str:
        allowed = {"stdio", "http", "both"}
        if v.lower() not in allowed:
            raise ValueError(f"transport must be one of {allowed}")
        return v.lower()

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return v.upper()


# ------------------------------------------------------------------ #
#  Connection config model
# ------------------------------------------------------------------ #

class ConnectionConfig:
    """Parsed database connection configuration."""

    def __init__(self, name: str, raw: dict) -> None:
        self.name = name
        self.type: str = raw.get("type", "").lower()
        self.description: str = raw.get("description", "")
        self.raw = raw  # Full raw dict for connector-specific fields

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def __repr__(self) -> str:
        return f"<ConnectionConfig name={self.name!r} type={self.type!r}>"


# ------------------------------------------------------------------ #
#  Loader function
# ------------------------------------------------------------------ #

_settings: Settings | None = None
_connections: dict[str, ConnectionConfig] | None = None


def load_settings() -> Settings:
    """Load and cache application settings."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def load_connections(path: str | None = None) -> dict[str, ConnectionConfig]:
    """
    Load and cache database connection configs from YAML.

    Args:
        path: Override path to connections.yaml.

    Returns:
        Dict mapping connection name -> ConnectionConfig.
    """
    global _connections
    if _connections is None:
        settings = load_settings()
        yaml_path = path or settings.connections_path
        data = _load_yaml(yaml_path)
        raw_connections: dict = data.get("connections", {})
        _connections = {
            name: ConnectionConfig(name, cfg)
            for name, cfg in raw_connections.items()
        }
    return _connections


def load_policies(path: str | None = None) -> dict:
    """
    Load raw policy configuration from YAML.

    Args:
        path: Override path to policies.yaml.

    Returns:
        Full parsed policy dict.
    """
    settings = load_settings()
    yaml_path = path or settings.policy_path
    return _load_yaml(yaml_path)
