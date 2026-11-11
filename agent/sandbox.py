"""Per-session sandbox folder and the only way file names from the model become paths.

Deck names are normalized ("Jinx Aggro" -> "jinx-aggro") and anything that could leave the
folder ("../x", "/etc/passwd", "a\\b", ".hidden") is rejected, not silently rewritten. Every
path is also resolved and checked to be inside the sandbox before it is used.
"""

from __future__ import annotations

import re
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")


class UnsafeNameError(ValueError):
    pass


def deck_slug(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafeNameError("a deck name is required")
    s = raw.strip().lower()
    if any(ch in s for ch in "/\\\x00") or ".." in s or s.startswith(".") or s.startswith("~"):
        raise UnsafeNameError(f'"{raw}" is not allowed: deck names cannot contain path characters (/ \\ .. or a leading dot)')
    s = re.sub(r"[\s_]+", "-", s)
    if not NAME_RE.match(s):
        raise UnsafeNameError(f'"{raw}" is not a valid deck name: use 1-40 letters, digits and hyphens, e.g. "jinx-aggro"')
    return s


class Sandbox:
    def __init__(self, root: Path):
        self.root = root.resolve()
        for sub in ("decks", "exports"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def path(self, sub: str, filename: str) -> Path:
        p = (self.root / sub / filename).resolve()
        if not p.is_relative_to(self.root / sub):
            raise UnsafeNameError(f"{filename!r} resolves outside the session folder")
        return p

    def deck_path(self, name: str) -> Path:
        return self.path("decks", f"{deck_slug(name)}.json")

    @property
    def db_path(self) -> Path:
        return self.root / "agent.db"

    @property
    def audit_path(self) -> Path:
        return self.root / "audit.jsonl"
