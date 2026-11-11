"""One agent session = one browser tab: its own sandbox folder, SQLite file, audit log and limits.

The rules index (KnowledgeBase + Catalog) is shared by every session in the process and is
reloaded in place after refresh_rules, so all sessions see the new data.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from riftbound import config
from riftbound.knowledge import KnowledgeBase

from .audit import AuditLog
from .catalog import Catalog
from .db import AgentDB
from .decks import DeckStore
from .sandbox import Sandbox

SEED_DIR = Path(__file__).parent / "seed"


class RulesStore:
    """Process-wide holder for the rules index, reloadable after a refresh."""

    def __init__(self):
        self._lock = threading.Lock()
        self._catalog: Catalog | None = None

    @property
    def catalog(self) -> Catalog:
        with self._lock:
            if self._catalog is None:
                self._catalog = Catalog(KnowledgeBase.load())
            return self._catalog

    def reload(self) -> Catalog:
        with self._lock:
            self._catalog = Catalog(KnowledgeBase.load())
            return self._catalog


RULES = RulesStore()


@dataclass
class AgentSession:
    id: str
    sandbox: Sandbox
    db: AgentDB
    decks: DeckStore
    audit: AuditLog
    store: RulesStore = RULES
    requests_used: int = 0
    request_limit: int = config.SESSION_REQUEST_LIMIT
    refreshes_used: int = 0
    last_version_check: float = 0.0
    # Rule passages retrieved by lookup_rule during the current request (for citation checking).
    retrieved: list[dict] = field(default_factory=list)
    # Decks first created during the current request; re-saving them is not overwriting the player's data.
    created_this_request: set[str] = field(default_factory=set)

    @property
    def catalog(self) -> Catalog:
        return self.store.catalog

    @property
    def kb(self) -> KnowledgeBase:
        return self.catalog.kb

    @classmethod
    def create(cls, root: Path | None = None, session_id: str | None = None, seed: bool = True, store: RulesStore = RULES) -> "AgentSession":
        sid = session_id or uuid.uuid4().hex[:12]
        sandbox = Sandbox((root or config.SANDBOX_ROOT) / sid)
        catalog = store.catalog
        sess = cls(
            id=sid,
            sandbox=sandbox,
            db=AgentDB(sandbox.db_path, catalog),
            decks=DeckStore(sandbox),
            audit=AuditLog(sandbox.audit_path, sid),
            store=store,
        )
        if seed:
            sess.seed()
        return sess

    def seed(self) -> None:
        """Copy the demo decks and matches into a new session so the demo works on first load."""
        for src in SEED_DIR.glob("*.json"):
            if src.name == "matches.json":
                continue
            dst = self.sandbox.deck_path(src.stem)
            if not dst.exists():
                shutil.copyfile(src, dst)
        matches = SEED_DIR / "matches.json"
        if matches.exists() and not self.db.all_matches():
            for m in json.loads(matches.read_text(encoding="utf-8")):
                self.db.log_match(m["deck"], m["opponent"], m["result"], m.get("notes", ""))

    def state_summary(self) -> dict:
        """Small snapshot of session state included in each prompt."""
        return {
            "saved_decks": [
                {k: d[k] for k in ("name", "legend", "champion", "format", "main_count")} for d in self.decks.list()
            ],
            "matches_logged": len(self.db.all_matches()),
            "requests_left": max(0, self.request_limit - self.requests_used),
        }

    def close(self) -> None:
        self.db.close()


def cleanup_old_sandboxes(max_age_hours: float = 24, root: Path | None = None) -> int:
    """Delete session folders untouched for a day, so a long-running deployment does not fill its disk."""
    root = root or config.SANDBOX_ROOT
    if not root.exists():
        return 0
    removed = 0
    for d in root.iterdir():
        if d.is_dir() and time.time() - d.stat().st_mtime > max_age_hours * 3600:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed
