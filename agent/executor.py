"""Run validated actions with safety tiers, confirmation gating and audit logging.

Status of an action record (shown as a badge in the UI):
    validated               passed validation, about to run
    awaiting_confirmation   destructive; paused until the user clicks Confirm or Cancel
    done | failed           ran (or tried to)
    blocked                 refused by policy (rate limit, disabled feature)
    cancelled               the user declined a destructive action
    skipped                 not run because an earlier action in the plan failed or was cancelled
    rejected                failed validation; never ran
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from typing import Any

from .actions import ActionError, ActionResult, ActionSpec
from .validator import ProposedAction

OBSERVATION_CHARS = 6000


@dataclass
class ActionRecord:
    id: str
    name: str
    args: dict
    tier: str = "read"
    domain: str = ""
    status: str = "validated"
    summary: str = ""
    data: Any = None
    changes: list[str] = field(default_factory=list)
    preview: list[str] = field(default_factory=list)
    error: str | None = None
    display: Any = None
    file: dict | None = None

    def observation(self) -> dict:
        """What the model sees about this action on its next step."""
        obs: dict = {"action": self.name, "status": self.status}
        if self.summary:
            obs["summary"] = self.summary
        if self.error:
            obs["error"] = self.error
        if self.changes:
            obs["changes"] = self.changes[:20]
        if self.data is not None:
            text = json.dumps(self.data, ensure_ascii=False, default=str)
            obs["result"] = self.data if len(text) <= OBSERVATION_CHARS else {"truncated_json": text[:OBSERVATION_CHARS]}
        return obs


class Executor:
    def __init__(self, session):
        self.s = session

    def _log(self, event: str, rec: ActionRecord, request_id: str, **kw) -> None:
        self.s.audit.log(event, request_id=request_id, action=rec.name, args=rec.args, tier=rec.tier, **kw)

    def rejected(self, pa: ProposedAction, request_id: str) -> ActionRecord:
        rec = ActionRecord(f"{request_id}-{pa.index}", pa.name, pa.raw_args if isinstance(pa.raw_args, dict) else {},
                           domain=pa.spec.domain if pa.spec else "", status="rejected", error="; ".join(pa.errors))
        self._log("rejected", rec, request_id, outcome="rejected", error=rec.error)
        return rec

    def prepare(self, pa: ProposedAction, request_id: str, rec_id: str) -> ActionRecord:
        spec: ActionSpec = pa.spec
        rec = ActionRecord(rec_id, spec.name, pa.args, domain=spec.domain)
        self._log("proposed", rec, request_id)
        rec.tier = spec.effective_tier(self.s, pa.args)
        self._log("validated", rec, request_id, outcome="validated")
        return rec

    def execute(self, rec: ActionRecord, request_id: str, confirmed: bool = False) -> ActionRecord:
        from .actions import REGISTRY

        spec = REGISTRY[rec.name]
        reason = spec.precheck(self.s, rec.args) if spec.precheck else None
        if reason:
            rec.status, rec.error = "blocked", reason
            self._log("failed", rec, request_id, outcome="blocked", error=reason)
            return rec

        if rec.tier == "destructive" and not confirmed:
            try:
                rec.preview = spec.preview(self.s, rec.args) if spec.preview else [f"Run {rec.name}"]
            except ActionError as exc:
                rec.status, rec.error = "failed", str(exc)
                self._log("failed", rec, request_id, outcome="failed", error=rec.error)
                return rec
            rec.status = "awaiting_confirmation"
            self._log("awaiting_confirmation", rec, request_id, outcome="awaiting_confirmation", preview=rec.preview)
            return rec

        if confirmed:
            self._log("confirmed", rec, request_id, outcome="confirmed")
        try:
            result: ActionResult = spec.handler(self.s, rec.args)
        except ActionError as exc:
            rec.status, rec.error = "failed", str(exc)
            self._log("failed", rec, request_id, outcome="failed", error=rec.error)
            return rec
        except Exception as exc:  # a bug, not the model's fault: report it without crashing the app
            rec.status, rec.error = "failed", f"internal error ({type(exc).__name__}: {exc})"
            self._log("failed", rec, request_id, outcome="failed", error=rec.error, trace=traceback.format_exc(limit=3))
            return rec
        rec.status = "done"
        rec.summary, rec.data, rec.changes, rec.display, rec.file = (
            result.summary, result.data, result.changes, result.display, result.file,
        )
        self._log("executed", rec, request_id, outcome="done", summary=rec.summary, changes=rec.changes)
        return rec

    def cancel(self, rec: ActionRecord, request_id: str) -> ActionRecord:
        rec.status = "cancelled"
        rec.summary = "Cancelled by the user; nothing was changed."
        self._log("cancelled", rec, request_id, outcome="cancelled")
        return rec

    def block(self, rec: ActionRecord, request_id: str, why: str) -> ActionRecord:
        rec.status, rec.error = "blocked", why
        self._log("failed", rec, request_id, outcome="blocked", error=why)
        return rec

    def skip(self, rec: ActionRecord, request_id: str, why: str) -> ActionRecord:
        rec.status, rec.summary = "skipped", why
        self._log("skipped", rec, request_id, outcome="skipped", error=why)
        return rec
