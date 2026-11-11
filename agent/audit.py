"""Append-only JSONL audit log: one line per action event, for auditability.

Events: proposed, validated, rejected, awaiting_confirmation, confirmed, cancelled, executed,
failed, skipped, plus llm_call / llm_error / request / limit for the surrounding loop.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

MAX_ARG_CHARS = 4000

EVENTS = (
    "request", "llm_call", "llm_error", "plan_rejected", "proposed", "validated", "rejected",
    "awaiting_confirmation", "confirmed", "cancelled", "executed", "failed", "skipped", "limit", "final",
)


def _clip(value):
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= MAX_ARG_CHARS:
        return value
    return {"truncated": True, "preview": text[:MAX_ARG_CHARS]}


class AuditLog:
    def __init__(self, path: Path, session_id: str):
        self.path = path
        self.session_id = session_id
        self._lock = threading.Lock()

    def log(
        self,
        event: str,
        *,
        request_id: str | None = None,
        action: str | None = None,
        args: dict | None = None,
        tier: str | None = None,
        outcome: str | None = None,
        error: str | None = None,
        **extra,
    ) -> dict:
        if event not in EVENTS:
            raise ValueError(f"unknown audit event {event!r}")
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session": self.session_id,
            "request": request_id,
            "event": event,
            "action": action,
            "args": _clip(args) if args is not None else None,
            "tier": tier,
            "outcome": outcome,
            "error": error,
        }
        entry.update({k: _clip(v) for k, v in extra.items()})
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def raw(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""
