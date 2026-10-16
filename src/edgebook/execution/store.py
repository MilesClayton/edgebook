"""Atomic session state plus an append-only execution event log."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from edgebook.execution.models import SessionState


class SessionStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.state_path = directory / "state.json"
        self.events_path = directory / "events.jsonl"

    def load(self) -> SessionState | None:
        if not self.state_path.is_file():
            return None
        return SessionState.model_validate_json(self.state_path.read_text())

    def save(self, state: SessionState, event: str, detail: dict[str, Any] | None = None) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump(mode="json")
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        os.replace(tmp, self.state_path)
        record = {
            "at": datetime.now(UTC).isoformat(),
            "event": event,
            "revision": state.revision,
            "detail": detail or {},
            # Keep a compact materialized point with every event.  The JSONL
            # log remains the source of truth while this makes the remote
            # dashboard able to draw the actual paper equity path.
            "state": {
                "cash": str(state.paper_cash),
                "reserved": str(state.paper_reserved),
                "equity": str(state.paper_equity),
                "realized_pnl": str(state.paper_realized_pnl),
                "fees": str(state.paper_fees),
                "open_positions": len(state.positions),
                "max_drawdown": str(state.paper_max_drawdown),
            },
        }
        with self.events_path.open("a") as events:
            events.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")

    def history(self, limit: int = 240) -> list[dict[str, Any]]:
        """Return recent valid event records without exposing local files."""
        if not self.events_path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for line in self.events_path.read_text().splitlines()[-limit:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records
