"""Card lookup and format legality, built from the local rules index (no LLM involved).

* resolve() turns a name the model wrote into exactly one official card, or explains why not.
* The banned lists are parsed from the Rules Hub legality passage and the Standard sets from
  Tournament Rules 601.3.c, so a rules refresh updates them without code changes.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from riftbound.knowledge import KnowledgeBase
from riftbound.parsing import norm_name

DOMAINS = ("Body", "Calm", "Chaos", "Fury", "Mind", "Order")
MAIN_DECK_TYPES = ("Unit", "Gear", "Spell")


class CardLookupError(ValueError):
    def __init__(self, message: str, suggestions: list[str] | None = None):
        super().__init__(message)
        self.suggestions = suggestions or []


def _key(name: str) -> str:
    """Loose key: case, curly quotes, punctuation and a leading "The" do not matter."""
    k = norm_name(name).replace(",", " ").replace("'", "")
    k = re.sub(r"^the\s+", "", k)
    return re.sub(r"\s+", " ", k).strip()


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def card_domains(card: dict) -> set[str]:
    """Colorless is shown as a domain in the gallery but means "no domain" (CR 103.2.b.5)."""
    return {d for d in card.get("domains", []) if d in DOMAINS}


@dataclass
class Legality:
    """Banned names per format plus any banned names that do not match a card (shown as warnings)."""

    banned: dict[str, set[str]] = field(default_factory=dict)  # format -> official card names
    unmatched: dict[str, list[str]] = field(default_factory=dict)
    updated: str = ""
    standard_sets: dict[str, str] = field(default_factory=dict)  # set code -> set name


class Catalog:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb
        self.cards: list[dict] = kb.cards
        self.by_name = {c["name"]: c for c in self.cards}
        self._index: dict[str, list[dict]] = {}
        for c in self.cards:
            keys = {_key(c["name"]), *(_key(a) for a in c.get("aliases", []))}
            for k in keys:
                self._index.setdefault(k, []).append(c)
        self._base: dict[str, list[dict]] = {}
        for c in self.cards:
            self._base.setdefault(_key(c["base_name"]), []).append(c)
        self.legality = self._parse_legality()

    # ------------------------------------------------------------------ lookup

    def resolve(self, name: str) -> dict:
        if not isinstance(name, str) or not name.strip():
            raise CardLookupError("A card name is required.")
        k = _key(name)
        hits = self._index.get(k) or []
        if len(hits) == 1:
            return hits[0]
        base = self._base.get(k) or []
        if len(hits) > 1 or len(base) > 1:
            options = sorted({c["name"] for c in hits or base})
            raise CardLookupError(f'"{name}" matches several cards; use the full name.', options)
        if len(base) == 1:
            return base[0]
        close = difflib.get_close_matches(k, list(self._index), n=4, cutoff=0.7)
        suggestions = sorted({self._index[c][0]["name"] for c in close})
        raise CardLookupError(f'No official card is named "{name}".', suggestions)

    # ------------------------------------------------------------------ legality

    def _parse_legality(self) -> Legality:
        leg = Legality()
        hub = next((p for p in self.kb.passages if p["source_kind"] == "legality"), None)
        if hub:
            leg.updated = hub.get("date") or ""
            # The page lists "Constructed Format Legality" then "2v2 Constructed Legality"; each has
            # optional "Legends", then "Cards" and "Battlefields" headings followed by one name per line.
            section, fmt = None, None
            for line in (l.strip() for l in hub["text"].splitlines()):
                low = line.lower()
                if low.startswith("2v2") and "legality" in low:
                    fmt = "2v2"
                    continue
                if "constructed format legality" in low:
                    fmt = "standard"
                    continue
                if low in ("legends", "cards", "battlefields"):
                    section = low
                    continue
                if not fmt or not section or not line or low.startswith(("last updated", "as above", "all sets", "the following")):
                    continue
                try:
                    card = self.resolve(line)
                except CardLookupError:
                    leg.unmatched.setdefault(fmt, []).append(line)
                    continue
                leg.banned.setdefault(fmt, set()).add(card["name"])
        leg.banned.setdefault("standard", set())
        # 2v2 bans everything Constructed bans plus its own list.
        leg.banned["2v2"] = leg.banned.get("2v2", set()) | leg.banned["standard"]
        for p in self.kb.passages:
            if p["source_kind"] == "tournament_rules" and "601.3.c" in p.get("rule_ids", []):
                for name, code in re.findall(r"601\.3\.c\.\d+\.\s+(.+?)\s*\(([A-Za-z]{2,4})\)", p["text"]):
                    leg.standard_sets[code.upper()] = name.strip()
        return leg

    def is_banned(self, card: dict, fmt: str = "standard") -> bool:
        return card["name"] in self.legality.banned.get(fmt, set())

    def card_sets(self, card: dict) -> set[str]:
        return {code.split("-")[0].upper() for code in card.get("codes", []) if code}

    # ------------------------------------------------------------------ deck-building helpers

    def legends_for(self, champion: str) -> list[dict]:
        k = _key(champion)
        return [c for c in self.cards if "Legend" in c["types"] and _key(c.get("champion_tag") or "") == k]

    def fits_identity(self, card: dict, identity: set[str]) -> bool:
        return card_domains(card) <= identity

    def compact(self, card: dict, fmt: str = "standard") -> dict:
        """Short card row for observations sent back to the model."""
        text = card.get("current_text") or ""
        row = {
            "name": card["name"],
            "type": "/".join(card["types"]),
            "domains": sorted(card_domains(card)) or ["Colorless"],
            "energy": _int(card.get("energy")),
            "might": _int(card.get("might")),
        }
        if card.get("supertypes"):
            row["supertype"] = "/".join(card["supertypes"])
        if card.get("champion_tag"):
            row["champion"] = card["champion_tag"]
        if fmt != "casual" and self.is_banned(card, fmt):
            row["banned"] = True
        row["text"] = text if len(text) <= 140 else text[:137] + "..."
        return row
