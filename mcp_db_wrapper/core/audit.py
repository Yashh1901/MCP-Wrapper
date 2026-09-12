"""Privacy-preserving audit events for MCP tool activity."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


class AuditLogger:
    """Append metadata-only events; SQL, values, and credentials are never recorded."""

    def __init__(self, path: str | None) -> None:
        self._path = Path(path) if path else None

    def record(self, *, tool: str, connection: str | None, outcome: str) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "tool": tool,
            "connection": connection,
            "outcome": outcome,
        }
        with self._path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, separators=(",", ":")) + "\n")
