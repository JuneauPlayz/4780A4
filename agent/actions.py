"""The allowlist: every action the agent can take, with its domain, safety tier and argument schema.

Nothing outside this registry can run. There is no shell, no arbitrary file path and no SQL from
the model; handlers only receive arguments that validator.py has already checked and normalized.

Tiers:
    read         runs immediately
    write        runs and reports exactly what changed
    destructive  pauses for an explicit Confirm / Cancel in the UI before running
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from riftbound import config

from .catalog import DOMAINS, CardLookupError
from .deckrules import FORMATS, compact_report, validate_deck
from .decks import EDIT_SECTIONS, EXPORTERS, DeckError, apply_edit, build_deck, deck_diff

TIERS = ("read", "write", "destructive")
CARD_TYPES = ("Unit", "Spell", "Gear", "Battlefield", "Legend", "Rune")
SUPERTYPES = ("Champion", "Signature", "Token", "Basic")


class ActionError(Exception):
    """A handled failure with a message that is safe to show the user and the model."""


@dataclass(frozen=True)
class Arg:
    type: str  # str | int | bool | enum | str_list | card_list | deck_name
    required: bool = False
    help: str = ""
    choices: tuple = ()
    max_len: int = 120
    min: int | None = None
    max: int | None = None
    max_items: int = 20
    default: Any = None


@dataclass
class ActionResult:
    summary: str
    data: Any = None  # what the model observes
    changes: list[str] = field(default_factory=list)  # for write/destructive actions
    display: Any = None  # richer payload for the UI (e.g. the full validation report)
    file: dict | None = None  # {"name", "mime", "content"} for downloads


@dataclass(frozen=True)
class ActionSpec:
    name: str
    domain: str  # database | files | validation | rules | network
    tier: str
    description: str
    args: dict[str, Arg]
    handler: Callable[..., ActionResult]
    # Optional: escalate the tier for specific arguments (save_deck over an existing deck).
    tier_for: Callable[..., str] | None = None
    # Optional: what a destructive action will do, shown on the Confirm / Cancel card.
    preview: Callable[..., list[str]] | None = None
    # Optional: policy check (rate limits, feature flags); returns a reason to block or None.
    precheck: Callable[..., str | None] | None = None

    def effective_tier(self, session, args: dict) -> str:
        return self.tier_for(session, args) if self.tier_for else self.tier


# ====================================================================== database


def _search_cards(s, a) -> ActionResult:
    try:
        res = s.db.search_cards(a)
    except (CardLookupError, ValueError) as exc:
        raise ActionError(str(exc)) from exc
    # Flipped Interaction, triggered by data: point out a choice the player has to make.
    legends: dict[str, list[str]] = {}
    for c in res["cards"]:
        if c["type"] == "Legend" and c.get("champion"):
            legends.setdefault(c["champion"], []).append(c["name"])
    for champ, names in legends.items():
        if len(names) > 1:
            res["notes"].append(
                f"{len(names)} Legends belong to {champ}: {', '.join(names)}. Unless the player already named one, "
                "ask which one with ask_user before building."
            )
    return ActionResult(f"{res['total_matches']} matching cards, showing {res['returned']}", res)


def _get_card(s, a) -> ActionResult:
    try:
        card = s.catalog.resolve(a["name"])
    except CardLookupError as exc:
        hint = f" Options: {', '.join(exc.suggestions)}." if exc.suggestions else ""
        raise ActionError(f"{exc}{hint}") from exc
    data = {
        **s.catalog.compact(card),
        "text": card.get("current_text") or "",
        "tags": card.get("tags", []),
        "sets": card.get("sets", []),
        "errata": bool(card.get("errata")),
        "banned_in_standard": s.catalog.is_banned(card, "standard"),
    }
    return ActionResult(f"Found {card['name']}", data)


def _log_match(s, a) -> ActionResult:
    deck = a["deck"]
    row = s.db.log_match(deck, a["opponent"].strip(), a["result"], (a.get("notes") or "").strip())
    note = "" if s.decks.exists(deck) else f" (note: no saved deck is named '{deck}')"
    return ActionResult(
        f"Logged a {row['result']} for {deck} vs {row['opponent']}{note}",
        {"match": row, "deck_saved": s.decks.exists(deck)},
        changes=[f"matches: +1 row #{row['id']} ({deck} vs {row['opponent']}: {row['result']})"],
    )


def _match_stats(s, a) -> ActionResult:
    stats = s.db.match_stats(a.get("deck"), a.get("opponent"))
    return ActionResult(f"{stats['games']} games found", stats)


# ====================================================================== files


def _save_tier(s, a) -> str:
    """Overwriting a deck that existed before this request needs confirmation; revising a draft saved
    earlier in the same request (e.g. after validate_deck found a problem) does not."""
    try:
        exists = s.decks.exists(a["name"])
    except ValueError:
        return "write"
    return "destructive" if exists and a["name"] not in s.created_this_request else "write"


def _build(s, a) -> dict:
    try:
        return build_deck(a, s.catalog)
    except DeckError as exc:
        raise ActionError(str(exc)) from exc


def _save_preview(s, a) -> list[str]:
    old = s.decks.load(a["name"])
    return [f"Overwrite saved deck '{old['name']}'. Changes:"] + deck_diff(old, _build(s, a))


def _save_deck(s, a) -> ActionResult:
    deck = _build(s, a)
    old = s.decks.load(deck["name"]) if s.decks.exists(deck["name"]) else None
    changes = deck_diff(old, deck)
    saved = s.decks.save(deck)
    if old is None:
        s.created_this_request.add(saved["name"])
    report = validate_deck(saved, s.catalog)
    verb = "Overwrote" if old else "Saved"
    return ActionResult(
        f"{verb} deck '{saved['name']}' ({sum(saved['main'].values())} main cards); quick check: {report['summary']}",
        {"deck": saved["name"], "saved": True, "overwrote": bool(old), "main": saved["main"], "quick_validation": compact_report(report)},
        changes=changes,
    )


def _edited(s, a) -> tuple[dict, dict]:
    old = _load(s, a["name"])
    if not a.get("add") and not a.get("remove"):
        raise ActionError("edit_deck needs at least one card in 'add' or 'remove'.")
    try:
        return old, apply_edit(old, a.get("section") or "main", a.get("add") or [], a.get("remove") or [], s.catalog)
    except DeckError as exc:
        raise ActionError(str(exc)) from exc


def _edit_tier(s, a) -> str:
    """Same rule as save_deck: changing a deck the player already had needs confirmation."""
    return "write" if a["name"] in s.created_this_request else "destructive"


def _edit_preview(s, a) -> list[str]:
    old, new = _edited(s, a)
    return [f"Change saved deck '{old['name']}':"] + deck_diff(old, new)


def _edit_deck(s, a) -> ActionResult:
    old, new = _edited(s, a)
    changes = deck_diff(old, new)
    saved = s.decks.save(new)
    report = validate_deck(saved, s.catalog)
    return ActionResult(
        f"Edited deck '{saved['name']}' ({sum(saved['main'].values())} main cards); quick check: {report['summary']}",
        {"deck": saved["name"], "main_count": sum(saved["main"].values()), "main": saved["main"], "quick_validation": compact_report(report)},
        changes=changes,
    )


def _load(s, name: str) -> dict:
    try:
        return s.decks.load(name)
    except DeckError as exc:
        raise ActionError(str(exc)) from exc


def _load_deck(s, a) -> ActionResult:
    deck = _load(s, a["name"])
    return ActionResult(f"Loaded '{deck['name']}'", {k: v for k, v in deck.items() if k not in ("created_at",)})


def _list_decks(s, a) -> ActionResult:
    decks = s.decks.list()
    return ActionResult(f"{len(decks)} saved deck(s)", {"decks": decks})


def _delete_preview(s, a) -> list[str]:
    d = _load(s, a["name"])
    return [f"Permanently delete deck '{d['name']}' ({d['legend']}, {sum(d['main'].values())} main cards). This cannot be undone."]


def _delete_deck(s, a) -> ActionResult:
    try:
        d = s.decks.delete(a["name"])
    except DeckError as exc:
        raise ActionError(str(exc)) from exc
    return ActionResult(f"Deleted deck '{d['name']}'", {"deleted": d["name"]}, changes=[f"decks: -{d['name']}.json"])


def _export_deck(s, a) -> ActionResult:
    deck = _load(s, a["name"])
    fmt = a.get("format") or "text"
    render, ext = EXPORTERS[fmt]
    content = render(deck)
    path = s.sandbox.path("exports", f"{deck['name']}.{ext}")
    existed = path.exists()
    path.write_text(content, encoding="utf-8")
    mime = {"txt": "text/plain", "csv": "text/csv", "json": "application/json"}[ext]
    return ActionResult(
        f"Exported '{deck['name']}' as {fmt} ({len(content)} bytes)",
        {"file": f"exports/{path.name}", "bytes": len(content), "preview": content[:600]},
        changes=[f"exports: {'replaced' if existed else 'created'} {path.name} ({len(content)} bytes)"],
        file={"name": path.name, "mime": mime, "content": content},
    )


# ====================================================================== validation


def _validate_deck(s, a) -> ActionResult:
    deck = _load(s, a["name"])
    report = validate_deck(deck, s.catalog, a.get("format"))
    verdict = "LEGAL" if report["legal"] else "NOT LEGAL"
    return ActionResult(f"'{deck['name']}' is {verdict} in {report['format']}: {report['summary']}", compact_report(report), display=report)


# ====================================================================== rules


def _lookup_rule(s, a) -> ActionResult:
    r = s.kb.retrieve(a["question"], k_core=4, k_other=2)
    passages = []
    for p in r.passages:
        s.retrieved.append(p)
        kind = {"core_rules": "CR", "tournament_rules": "TR"}.get(p["source_kind"])
        text = p["text"] if len(p["text"]) <= 1500 else p["text"][:1500].rsplit("\n", 1)[0] + "\n[...truncated]"
        passages.append({
            "source": f"{p['source_title']} ({p.get('doc_version') or p.get('date') or 'undated'})",
            "cite_as": f"[{kind} <rule number>]" if kind else "quote the source title",
            "title": p["title"],
            "text": text,
        })
    cards = [s.kb.card_summary(c) for c in r.cards[:3]]
    return ActionResult(f"Retrieved {len(passages)} rule passages", {"passages": passages, "cards": cards})


# ====================================================================== network


def _version_precheck(s, a) -> str | None:
    wait = s.last_version_check + config.VERSION_CHECK_COOLDOWN_SECONDS - time.time()
    return f"rate limited: try again in {int(wait) + 1}s" if wait > 0 else None


def _check_rules_version(s, a) -> ActionResult:
    from riftbound import parsing
    from riftbound.sources import RULES_HUB_URL, discover_sources, fetch

    s.last_version_check = time.time()
    try:
        hub = fetch(RULES_HUB_URL, timeout=20).text
    except Exception as exc:  # network errors become a readable failure
        raise ActionError(f"Could not reach Riot's Rules Hub: {type(exc).__name__}") from exc
    manifest = s.kb.manifest
    docs = {d["kind"]: d for d in manifest["documents"]}
    changes = []
    for src in discover_sources(hub):
        old = docs.get(src.kind)
        if src.kind in ("core_rules", "tournament_rules") and old and old["url"] != src.url:
            changes.append(f"{src.title}: a new PDF is linked")
    leg = parsing.legality_passage(hub, RULES_HUB_URL, None)
    local = next((p for p in s.kb.passages if p["source_kind"] == "legality"), None)
    if leg and local and hashlib.sha1(leg["text"].encode()).hexdigest() != hashlib.sha1(local["text"].encode()).hexdigest():
        changes.append(f"Banned list changed (Rules Hub date {leg.get('date')})")
    core = manifest["core_rules"]
    return ActionResult(
        "Rules are up to date" if not changes else f"{len(changes)} update(s) available",
        {
            "up_to_date": not changes,
            "changes": changes,
            "local_core_rules": core["last_updated"],
            "local_effective": core["effective_date"],
            "index_fetched": manifest["fetched_at"][:10],
        },
    )


def _refresh_stamp():
    return config.SANDBOX_ROOT / ".last_refresh"


def _refresh_precheck(s, a) -> str | None:
    if not config.ALLOW_REFRESH:
        return "refresh_rules is disabled on this deployment (ALLOW_REFRESH=false)"
    if s.refreshes_used >= 1:
        return "refresh_rules can run once per session"
    stamp = _refresh_stamp()
    if stamp.exists():
        wait = float(stamp.read_text() or 0) + config.REFRESH_COOLDOWN_SECONDS - time.time()
        if wait > 0:
            return f"rate limited: the rules were refreshed recently; try again in {int(wait // 60) + 1} min"
    return None


def _refresh_preview(s, a) -> list[str]:
    core = s.kb.manifest["core_rules"]
    return [
        "Download the current Core Rules, Tournament Rules, FAQs, errata, banned list and Card Gallery from Riot "
        "and replace the local rules index used by every session.",
        f"Current index: Core Rules {core['last_updated']} (fetched {s.kb.manifest['fetched_at'][:10]}). Takes about a minute.",
    ]


def _refresh_rules(s, a) -> ActionResult:
    from riftbound.refresh import refresh

    s.refreshes_used += 1
    stamp = _refresh_stamp()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(str(time.time()))
    before = s.kb.manifest["core_rules"]["last_updated"]
    log: list[str] = []
    try:
        manifest = refresh(log=log.append)
    except Exception as exc:
        raise ActionError(f"Refresh failed and the existing index was kept: {exc}") from exc
    catalog = s.store.reload()
    s.db.catalog = catalog
    s.db.load_cards()
    after = manifest["core_rules"]["last_updated"]
    return ActionResult(
        f"Rules index rebuilt: Core Rules {after}" + (" (unchanged)" if before == after else f" (was {before})"),
        {"core_rules": after, "previous": before, "counts": manifest["counts"]},
        changes=[f"data/: rebuilt {manifest['counts']['passages']} passages, {manifest['counts']['cards']} cards"] + log[-3:],
    )


# ====================================================================== registry

DECK_NAME = Arg("deck_name", required=True, help='saved deck name, e.g. "jinx-aggro"')
CARD_LIST_HELP = 'list of {"card": "<official name>", "count": <n>}'

REGISTRY: dict[str, ActionSpec] = {
    s.name: s
    for s in [
        ActionSpec(
            "search_cards", "database", "read",
            "Search the official card database (SQLite). Use 'legend' to get only cards that deck may contain. Banned cards are hidden unless include_banned.",
            {
                "name": Arg("str", help="part of a card name"),
                "text": Arg("str", help="words in the rules text, e.g. 'discard'"),
                "types": Arg("str_list", choices=CARD_TYPES, max_items=6, help="card types"),
                "domain": Arg("enum", choices=DOMAINS),
                "legend": Arg("str", help="a Legend's full name; filters to its Domain Identity"),
                "tag": Arg("str", help="a tag or champion, e.g. 'Jinx'"),
                "supertype": Arg("enum", choices=SUPERTYPES),
                "max_energy": Arg("int", min=0, max=20),
                "include_banned": Arg("bool", default=False),
                "format": Arg("enum", choices=FORMATS, default="standard"),
                "limit": Arg("int", min=1, max=40, default=20),
            },
            _search_cards,
        ),
        ActionSpec(
            "get_card", "database", "read",
            "Get one card's official text (errata applied), stats, tags and banned status.",
            {"name": Arg("str", required=True)},
            _get_card,
        ),
        ActionSpec(
            "log_match", "database", "write",
            "Record a game result in the match log.",
            {
                "deck": DECK_NAME,
                "opponent": Arg("str", required=True, max_len=80, help="opponent's legend or deck, e.g. 'Ahri'"),
                "result": Arg("enum", required=True, choices=("win", "loss", "draw")),
                "notes": Arg("str", max_len=300),
            },
            _log_match,
        ),
        ActionSpec(
            "match_stats", "database", "read",
            "Win/loss statistics from the match log, overall or filtered by deck and/or opponent.",
            {"deck": Arg("deck_name"), "opponent": Arg("str", max_len=80)},
            _match_stats,
        ),
        ActionSpec(
            "save_deck", "files", "write",
            "Save a deck file. The main list must include the Chosen Champion and total 40 cards for Standard. "
            "Overwriting an existing deck requires the user's confirmation.",
            {
                "name": DECK_NAME,
                "format": Arg("enum", choices=FORMATS, default="standard"),
                "legend": Arg("str", required=True, help="the Champion Legend's full name"),
                "champion": Arg("str", required=True, help="the Chosen Champion unit's full name"),
                "main": Arg("card_list", required=True, max_items=40, help=CARD_LIST_HELP),
                "runes": Arg("card_list", required=True, max_items=6, help=CARD_LIST_HELP),
                "battlefields": Arg("str_list", required=True, max_items=6, help="3 battlefield names"),
                "sideboard": Arg("card_list", max_items=10, help=CARD_LIST_HELP),
                "notes": Arg("str", max_len=300),
            },
            _save_deck,
            tier_for=_save_tier,
            preview=_save_preview,
        ),
        ActionSpec(
            "edit_deck", "files", "write",
            "Change part of a saved deck: remove and/or add cards in one section. Use this to fix a deck after "
            "validate_deck (e.g. add 3 cards) instead of saving the whole list again. Editing a deck the player "
            "already had requires confirmation.",
            {
                "name": DECK_NAME,
                "section": Arg("enum", choices=EDIT_SECTIONS, default="main"),
                "add": Arg("card_list", max_items=20, help=CARD_LIST_HELP),
                "remove": Arg("card_list", max_items=20, help=CARD_LIST_HELP),
            },
            _edit_deck,
            tier_for=_edit_tier,
            preview=_edit_preview,
        ),
        ActionSpec("load_deck", "files", "read", "Read a saved deck's full card list.", {"name": DECK_NAME}, _load_deck),
        ActionSpec("list_decks", "files", "read", "List saved decks.", {}, _list_decks),
        ActionSpec(
            "delete_deck", "files", "destructive",
            "Delete a saved deck file (asks the user to confirm).",
            {"name": DECK_NAME},
            _delete_deck,
            preview=_delete_preview,
        ),
        ActionSpec(
            "export_deck", "files", "write",
            "Write a deck to a downloadable file: text decklist, json or csv.",
            {"name": DECK_NAME, "format": Arg("enum", choices=tuple(EXPORTERS), default="text")},
            _export_deck,
        ),
        ActionSpec(
            "validate_deck", "validation", "read",
            "Check a saved deck against the official deck-construction rules and banned list (deterministic code, cites rule numbers).",
            {"name": DECK_NAME, "format": Arg("enum", choices=FORMATS)},
            _validate_deck,
        ),
        ActionSpec(
            "lookup_rule", "rules", "read",
            "Retrieve official rules passages (Core Rules, Tournament Rules, FAQs, errata) relevant to a question.",
            {"question": Arg("str", required=True, max_len=300)},
            _lookup_rule,
        ),
        ActionSpec(
            "check_rules_version", "network", "read",
            "Contact Riot's Rules Hub and report whether the local rules index is current. Rate limited.",
            {},
            _check_rules_version,
            precheck=_version_precheck,
        ),
        ActionSpec(
            "refresh_rules", "network", "destructive",
            "Re-download Riot's rules and card data and rebuild the local index (asks to confirm; at most once per session and once per hour overall).",
            {},
            _refresh_rules,
            preview=_refresh_preview,
            precheck=_refresh_precheck,
        ),
    ]
}


def catalog_text() -> str:
    """The ACTION_CATALOG prompt block, generated from the registry so it never drifts from the code."""
    lines = []
    for spec in REGISTRY.values():
        tier = spec.tier + (" (destructive when it changes a deck the player already had)" if spec.tier_for else "")
        lines.append(f"- {spec.name}  [{spec.domain}; {tier}]: {spec.description}")
        for name, arg in spec.args.items():
            t = arg.type
            if arg.choices:
                t += " " + "|".join(arg.choices)
            if arg.min is not None or arg.max is not None:
                t += f" {arg.min}..{arg.max}"
            req = "required" if arg.required else "optional"
            lines.append(f"    {name}: {t}, {req}" + (f" - {arg.help}" if arg.help else ""))
        if not spec.args:
            lines.append("    (no args)")
    return "\n".join(lines)
