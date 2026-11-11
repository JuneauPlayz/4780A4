"""Deck files: one JSON file per deck in the session sandbox.

A deck stores official card names only. Names the model writes are resolved through the
Catalog when the deck is saved, so a typo is reported (with suggestions) instead of saved.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

from .catalog import CardLookupError, Catalog
from .sandbox import Sandbox, deck_slug

SECTIONS = ("main", "runes", "sideboard")


class DeckError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve(catalog: Catalog, name: str, where: str, errors: list[str]) -> str | None:
    try:
        return catalog.resolve(name)["name"]
    except CardLookupError as exc:
        hint = f" Did you mean: {', '.join(exc.suggestions)}?" if exc.suggestions else ""
        errors.append(f"{where}: {exc}{hint}")
        return None


def build_deck(args: dict, catalog: Catalog) -> dict:
    """Validated save_deck args -> canonical deck dict. Raises DeckError listing every bad name."""
    errors: list[str] = []
    deck = {
        "name": deck_slug(args["name"]),
        "format": args.get("format") or "standard",
        "legend": _resolve(catalog, args["legend"], "legend", errors),
        "champion": _resolve(catalog, args["champion"], "champion", errors),
        "main": {},
        "runes": {},
        "battlefields": [],
        "sideboard": {},
        "notes": args.get("notes") or "",
    }
    for section in SECTIONS:
        for entry in args.get(section) or []:
            name = _resolve(catalog, entry["card"], section, errors)
            if name:
                deck[section][name] = deck[section].get(name, 0) + entry["count"]
    for bf in args.get("battlefields") or []:
        name = _resolve(catalog, bf, "battlefields", errors)
        if name:
            deck["battlefields"].append(name)
    if errors:
        raise DeckError("Some card names are not official cards, so nothing was saved:\n- " + "\n- ".join(errors))
    return deck


EDIT_SECTIONS = ("main", "sideboard", "runes", "battlefields")


def apply_edit(deck: dict, section: str, add: list[dict], remove: list[dict], catalog: Catalog) -> dict:
    """Return a copy of `deck` with cards removed from and added to one section (removals first).

    Small deltas ("add 3 Hextech Ray") are far easier for a model to get right than rewriting a
    40-card list, which is how a fix used to turn 37 cards into 37 again.
    """
    errors: list[str] = []
    new = json.loads(json.dumps(deck))
    if section == "battlefields":
        bfs = list(new.get("battlefields") or [])
        for entry in remove:
            name = _resolve(catalog, entry["card"], "remove", errors)
            if name and name in bfs:
                bfs.remove(name)
            elif name:
                errors.append(f"remove: {name} is not one of this deck's battlefields ({', '.join(bfs)})")
        for entry in add:
            name = _resolve(catalog, entry["card"], "add", errors)
            if name:
                bfs.append(name)
        new["battlefields"] = bfs
    else:
        cards = dict(new.get(section) or {})
        for entry in remove:
            name = _resolve(catalog, entry["card"], "remove", errors)
            if not name:
                continue
            have = cards.get(name, 0)
            if have < entry["count"]:
                errors.append(f"remove: the {section} has {have} {name}, so {entry['count']} cannot be removed")
                continue
            cards[name] = have - entry["count"]
            if not cards[name]:
                del cards[name]
        for entry in add:
            name = _resolve(catalog, entry["card"], "add", errors)
            if name:
                cards[name] = cards.get(name, 0) + entry["count"]
        new[section] = cards
        if section in ("main", "sideboard"):
            # Refuse to create a copy-limit problem (CR 103.3.b; Unique: CR 825.3.a). The model cannot see the
            # whole deck in a diff, and without this it kept adding a 4th and 5th copy of a card it already had.
            together = {n: new["main"].get(n, 0) + new.get("sideboard", {}).get(n, 0) for n in {*new["main"], *new.get("sideboard", {})}}
            for entry in add:
                name = _resolve(catalog, entry["card"], "add", [])
                if not name:
                    continue
                limit = 1 if "[Unique]" in (catalog.by_name[name].get("current_text") or "") else 3
                if together.get(name, 0) > limit:
                    room = sorted(n for n, k in new["main"].items() if k < 3 and n != name and "[Unique]" not in (catalog.by_name.get(n, {}).get("current_text") or ""))
                    errors.append(
                        f"add: {name} would have {together[name]} copies; the limit is {limit} (CR 103.3.b). "
                        f"It already has {together[name] - entry['count']}. Cards in this deck with room for more copies: "
                        + (", ".join(room) if room else "none; add a new card from the search results")
                    )
    if errors:
        raise DeckError("Nothing was changed:\n- " + "\n- ".join(errors))
    return new


def deck_diff(old: dict | None, new: dict) -> list[str]:
    """Human-readable list of what a save changes."""
    if old is None:
        return [f"new deck '{new['name']}': {sum(new['main'].values())} main, {sum(new['runes'].values())} runes, "
                f"{len(new['battlefields'])} battlefields, {sum(new['sideboard'].values())} sideboard"]
    out: list[str] = []
    for field in ("format", "legend", "champion"):
        if old.get(field) != new.get(field):
            out.append(f"{field}: {old.get(field)} -> {new.get(field)}")
    for section in SECTIONS:
        a, b = old.get(section, {}), new.get(section, {})
        for card in sorted(set(a) | set(b)):
            if a.get(card, 0) != b.get(card, 0):
                delta = b.get(card, 0) - a.get(card, 0)
                out.append(f"{section}: {'+' if delta > 0 else ''}{delta} {card} ({a.get(card, 0)} -> {b.get(card, 0)})")
    for bf in sorted(set(old.get("battlefields", [])) - set(new["battlefields"])):
        out.append(f"battlefields: -{bf}")
    for bf in sorted(set(new["battlefields"]) - set(old.get("battlefields", []))):
        out.append(f"battlefields: +{bf}")
    if old.get("notes", "") != new.get("notes", ""):
        out.append("notes changed")
    return out or ["no changes"]


class DeckStore:
    def __init__(self, sandbox: Sandbox):
        self.sb = sandbox

    def exists(self, name: str) -> bool:
        return self.sb.deck_path(name).exists()

    def load(self, name: str) -> dict:
        path = self.sb.deck_path(name)
        if not path.exists():
            have = ", ".join(d["name"] for d in self.list()) or "none"
            raise DeckError(f"No saved deck named '{deck_slug(name)}'. Saved decks: {have}.")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, deck: dict) -> dict:
        path = self.sb.deck_path(deck["name"])
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        deck = {**deck, "created_at": (old or {}).get("created_at") or now(), "updated_at": now()}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # atomic, so a crash never leaves half a deck
        return deck

    def delete(self, name: str) -> dict:
        deck = self.load(name)
        self.sb.deck_path(name).unlink()
        return deck

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.sb.root.joinpath("decks").glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({
                "name": d.get("name", p.stem),
                "legend": d.get("legend"),
                "champion": d.get("champion"),
                "format": d.get("format"),
                "main_count": sum(d.get("main", {}).values()),
                "updated_at": d.get("updated_at"),
            })
        return out


def export_text(deck: dict) -> str:
    lines = [f"# {deck['name']} ({deck.get('format', 'standard')})", f"Legend: {deck['legend']}", f"Chosen Champion: {deck['champion']}", ""]
    for title, section in (("Main Deck", "main"), ("Rune Deck", "runes"), ("Sideboard", "sideboard")):
        cards = deck.get(section) or {}
        if not cards:
            continue
        lines.append(f"{title} ({sum(cards.values())})")
        lines += [f"{n} {c}" for c, n in sorted(cards.items())]
        lines.append("")
    lines.append(f"Battlefields ({len(deck.get('battlefields', []))})")
    lines += [f"1 {b}" for b in deck.get("battlefields", [])]
    return "\n".join(lines) + "\n"


def export_csv(deck: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "count", "card"])
    w.writerow(["legend", 1, deck["legend"]])
    w.writerow(["champion", 1, deck["champion"]])
    for section in SECTIONS:
        for card, n in sorted((deck.get(section) or {}).items()):
            w.writerow([section, n, card])
    for b in deck.get("battlefields", []):
        w.writerow(["battlefield", 1, b])
    return buf.getvalue()


def export_json(deck: dict) -> str:
    return json.dumps({k: v for k, v in deck.items() if k not in ("created_at", "updated_at")}, indent=1, ensure_ascii=False)


EXPORTERS = {"text": (export_text, "txt"), "csv": (export_csv, "csv"), "json": (export_json, "json")}
