"""Per-session SQLite database: the official card list (read-only use) and the player's match log.

All queries use bound parameters; nothing the model writes is ever spliced into SQL text.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .catalog import MAIN_DECK_TYPES, Catalog, _int, card_domains

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    name TEXT PRIMARY KEY,
    types TEXT NOT NULL,          -- "|Unit|" style, so LIKE '%|Unit|%' matches one type exactly
    supertypes TEXT NOT NULL,
    domains TEXT NOT NULL,
    tags TEXT NOT NULL,
    champion_tag TEXT,
    energy INTEGER,
    might INTEGER,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    played_at TEXT NOT NULL,
    deck TEXT NOT NULL,
    opponent TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('win', 'loss', 'draw')),
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS matches_deck ON matches(deck);
"""


def _bar(values) -> str:
    return "|" + "|".join(values) + "|"


class AgentDB:
    def __init__(self, path: Path, catalog: Catalog):
        self.path = path
        self.catalog = catalog
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.load_cards()

    def load_cards(self) -> None:
        """(Re)load the card table from the current rules index, e.g. after refresh_rules."""
        with self.conn:
            self.conn.execute("DELETE FROM cards")
            self.conn.executemany(
                "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        c["name"], _bar(c["types"]), _bar(c.get("supertypes", [])), _bar(sorted(card_domains(c))),
                        _bar(c.get("tags", [])), c.get("champion_tag"), _int(c.get("energy")), _int(c.get("might")),
                        c.get("current_text") or "",
                    )
                    for c in self.catalog.cards
                ],
            )

    # ------------------------------------------------------------------ cards

    def search_cards(self, f: dict) -> dict:
        where, params = [], []
        if f.get("name"):
            where.append("name LIKE ?")
            params.append(f"%{f['name']}%")
        if f.get("text"):
            where.append("text LIKE ?")
            params.append(f"%{f['text']}%")
        if f.get("types"):
            where.append("(" + " OR ".join("types LIKE ?" for _ in f["types"]) + ")")
            params += [f"%|{t}|%" for t in f["types"]]
        if f.get("domain"):
            where.append("domains LIKE ?")
            params.append(f"%|{f['domain']}|%")
        if f.get("tag"):
            where.append("(tags LIKE ? OR champion_tag = ?)")
            params += [f"%|{f['tag']}|%", f["tag"]]
        if f.get("supertype"):
            where.append("supertypes LIKE ?")
            params.append(f"%|{f['supertype']}|%")
        if f.get("max_energy") is not None:
            where.append("energy <= ?")
            params.append(f["max_energy"])
        sql = "SELECT name FROM cards" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY energy IS NULL, energy, name"
        names = [r["name"] for r in self.conn.execute(sql, params)]
        cards = [self.catalog.by_name[n] for n in names]

        fmt = f.get("format") or "standard"
        notes = []
        if f.get("legend"):
            # Main Deck cards this legend's deck may contain: Units, Gear and Spells (CR 103.3) inside its
            # Domain Identity (CR 103.2.b), no tokens, and signature cards only for its own champion (CR 103.3.d.2).
            legend = self.catalog.resolve(f["legend"])
            if "Legend" not in legend["types"]:
                raise ValueError(f"{legend['name']} is not a Legend.")
            identity = card_domains(legend)
            cards = [
                c for c in cards
                if card_domains(c) <= identity
                and "Token" not in c.get("supertypes", [])
                and set(c["types"]) & set(MAIN_DECK_TYPES)
                and ("Signature" not in c.get("supertypes", []) or c.get("champion_tag") == legend.get("champion_tag"))
            ]
            notes.append(f"Filtered to the Domain Identity of {legend['name']}: {', '.join(sorted(identity))}.")
        if fmt != "casual" and not f.get("include_banned"):
            before = len(cards)
            cards = [c for c in cards if not self.catalog.is_banned(c, fmt)]
            if before != len(cards):
                notes.append(f"{before - len(cards)} banned card(s) hidden ({fmt}).")
        limit = f.get("limit") or 20
        return {
            "total_matches": len(cards),
            "returned": min(limit, len(cards)),
            "notes": notes,
            "cards": [self.catalog.compact(c, fmt) for c in cards[:limit]],
        }

    # ------------------------------------------------------------------ matches

    def log_match(self, deck: str, opponent: str, result: str, notes: str = "") -> dict:
        played_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO matches (played_at, deck, opponent, result, notes) VALUES (?,?,?,?,?)",
                (played_at, deck, opponent, result, notes),
            )
        return dict(self.conn.execute("SELECT * FROM matches WHERE id = ?", (cur.lastrowid,)).fetchone())

    def match_stats(self, deck: str | None = None, opponent: str | None = None) -> dict:
        where, params = [], []
        if deck:
            where.append("deck = ?")
            params.append(deck)
        if opponent:
            where.append("opponent LIKE ?")
            params.append(f"%{opponent}%")
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        def grouped(col: str) -> list[dict]:
            rows = self.conn.execute(
                f"SELECT {col} AS key, COUNT(*) AS games, SUM(result='win') AS wins, SUM(result='loss') AS losses, "
                f"SUM(result='draw') AS draws FROM matches{clause} GROUP BY {col} ORDER BY games DESC, key",
                params,
            ).fetchall()
            return [{**dict(r), "win_rate": round(r["wins"] / r["games"], 3)} for r in rows]

        recent = [dict(r) for r in self.conn.execute(f"SELECT * FROM matches{clause} ORDER BY id DESC LIMIT 10", params)]
        by_deck = grouped("deck")
        total = sum(r["games"] for r in by_deck)
        return {"games": total, "by_deck": by_deck, "by_opponent": grouped("opponent"), "recent": recent}

    def all_matches(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM matches ORDER BY id DESC")]

    def close(self) -> None:
        self.conn.close()
