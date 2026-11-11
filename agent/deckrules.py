"""Deterministic deck validation (no LLM). Every check cites the rule it enforces.

Rules come from the indexed Core Rules (CR 103 Deck Construction, CR 487/488 1v1 modes,
CR 825 Unique) and Tournament Rules (TR 601 Constructed). Each cited number is resolved with
the A3 CitationChecker, so a check whose rule disappears after a rules refresh shows up as
unverified instead of silently pointing at the wrong rule.

Formats:
    standard  sanctioned Constructed: exactly 40 main, sideboard <= 10, banned list, Standard sets
    casual    Core Rules only: at least 40 main, no banned list or set rotation
"""

from __future__ import annotations

from collections import Counter

from riftbound.explainer import CitationChecker
from riftbound.knowledge import Retrieval

from .catalog import MAIN_DECK_TYPES, Catalog, card_domains

FORMATS = ("standard", "casual")

# check id -> (title, rule references). Kept in one table so tests can verify every reference exists.
CHECKS: dict[str, tuple[str, list[str]]] = {
    "legend": ("Exactly one Champion Legend", ["CR 103", "CR 103.2"]),
    "main_types": ("Main Deck holds only Units, Gear and Spells", ["CR 103.3", "CR 185"]),
    "main_size": ("Main Deck size", ["CR 103.3", "TR 601.1.b"]),
    "champion": ("Chosen Champion matches the Legend", ["CR 103.3.a.2", "CR 103.3.a.3"]),
    "domain_identity": ("Cards fit the Legend's Domain Identity", ["CR 103.2.b.2", "CR 103.2.b.3", "CR 103.2.b.4", "CR 103.2.b.5", "CR 103.3.c"]),
    "copies": ("At most 3 copies of each named card", ["CR 103.3.b", "CR 103.3.b.1", "TR 601.1.c.3"]),
    "unique": ("Unique cards: at most 1 copy", ["CR 825.3.a"]),
    "signature": ("At most 3 Signature cards, all for the Legend's champion", ["CR 103.3.d.1", "CR 103.3.d.2"]),
    "runes": ("Rune Deck of 12 Runes in the Domain Identity", ["CR 103.4.a", "CR 103.4.a.1"]),
    "battlefields": ("3 Battlefields with different names", ["CR 487.4.a", "CR 103.5.c"]),
    "sideboard": ("Sideboard of at most 10 valid Main Deck cards", ["TR 601.1.c.1", "TR 601.1.c.2"]),
    "banned": ("No banned cards", ["TR 601.2.d"]),
    "sets": ("Every card is from a Standard set", ["TR 601.2.a", "TR 601.3.c"]),
}
STANDARD_ONLY = {"sideboard", "banned", "sets"}


def _delta(n: int, want: int) -> str:
    """Spell out the fix so the model does not have to do the arithmetic."""
    return f"add {want - n}" if n < want else f"remove {n - want}"


def _fmt_list(cards) -> str:
    return ", ".join(sorted(cards))


def validate_deck(deck: dict, catalog: Catalog, fmt: str | None = None) -> dict:
    fmt = fmt or deck.get("format") or "standard"
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    get = catalog.by_name.get
    problems: dict[str, list[str]] = {k: [] for k in CHECKS}
    warnings: list[str] = []

    main: Counter = Counter(deck.get("main") or {})
    side: Counter = Counter(deck.get("sideboard") or {})
    runes: Counter = Counter(deck.get("runes") or {})
    bfs: list[str] = list(deck.get("battlefields") or [])
    unknown = [n for n in [*main, *side, *runes, *bfs, deck.get("legend"), deck.get("champion")] if n and not get(n)]
    if unknown:
        warnings.append(f"Not in the current card list (ignored by the checks): {_fmt_list(set(unknown))}")

    # Legend and Domain Identity (CR 103.2)
    legend = get(deck.get("legend") or "")
    identity: set[str] = set()
    if not legend:
        problems["legend"].append("The deck has no Champion Legend.")
    elif "Legend" not in legend["types"]:
        problems["legend"].append(f"{legend['name']} is a {'/'.join(legend['types'])}, not a Legend.")
        legend = None
    else:
        identity = card_domains(legend)
    champ_tag = legend.get("champion_tag") if legend else None

    # Main Deck contents and size (CR 103.3, TR 601.1.b)
    for name, n in main.items():
        c = get(name)
        if c and (not set(c["types"]) & set(MAIN_DECK_TYPES) or "Token" in c.get("supertypes", [])):
            if not c["types"]:
                problems["main_types"].append(f"{name} has no card type in Riot's Card Gallery data, so it cannot be confirmed as a Unit, Gear or Spell.")
                continue
            kind = "a token" if "Token" in c.get("supertypes", []) else f"a {'/'.join(c['types'])}"
            problems["main_types"].append(f"{name} is {kind} and cannot be in the Main Deck.")
    size = sum(main.values())
    if fmt == "standard" and size != 40:
        problems["main_size"].append(f"Main Deck has {size} cards; Standard requires exactly 40 ({_delta(size, 40)}).")
    elif fmt == "casual" and size < 40:
        problems["main_size"].append(f"Main Deck has {size} cards; it needs at least 40 ({_delta(size, 40)}).")

    # Chosen Champion (CR 103.3.a.2-3)
    champ = get(deck.get("champion") or "")
    if not champ:
        problems["champion"].append("The deck has no Chosen Champion.")
    else:
        if not ("Unit" in champ["types"] and "Champion" in champ.get("supertypes", [])):
            problems["champion"].append(f"{champ['name']} is not a Champion unit.")
        elif legend and champ.get("champion_tag") != champ_tag:
            problems["champion"].append(
                f"{champ['name']} has champion tag {champ.get('champion_tag')}, but the Legend {legend['name']} needs {champ_tag}."
            )
        if main.get(champ["name"], 0) < 1:
            problems["champion"].append(f"The Chosen Champion {champ['name']} must be one of the Main Deck cards.")

    # Domain Identity for every card in the deck (CR 103.2.b, 103.3.c, 103.4.a.1)
    if legend:
        for section, cards in (("Main Deck", main), ("sideboard", side)):
            off = [n for n in cards if get(n) and not card_domains(get(n)) <= identity]
            if off:
                problems["domain_identity"].append(
                    f"{section} cards outside {'/'.join(sorted(identity))}: "
                    + ", ".join(f"{n} ({'/'.join(sorted(card_domains(get(n))))})" for n in sorted(off))
                )

    # Copies (CR 103.3.b; Standard counts main + sideboard together, TR 601.1.c.3) and Unique (CR 825.3.a)
    together = main + side if fmt == "standard" else main
    over = {n: k for n, k in together.items() if k > 3}
    if over:
        problems["copies"].append("More than 3 copies: " + ", ".join(f"{n} x{k} (remove {k - 3})" for n, k in sorted(over.items())))
    uniq = {n: k for n, k in together.items() if k > 1 and get(n) and "[Unique]" in (get(n).get("current_text") or "")}
    if uniq:
        problems["unique"].append("Unique cards with more than 1 copy: " + ", ".join(f"{n} x{k}" for n, k in sorted(uniq.items())))

    # Signature cards (CR 103.3.d)
    sigs = {n: k for n, k in main.items() if get(n) and "Signature" in get(n).get("supertypes", [])}
    if sum(sigs.values()) > 3:
        problems["signature"].append(f"{sum(sigs.values())} Signature cards ({_fmt_list(sigs)}); the limit is 3 in total.")
    wrong = [n for n in sigs if get(n).get("champion_tag") != champ_tag]
    if wrong and legend:
        problems["signature"].append(f"Signature cards for a different champion than {champ_tag}: {_fmt_list(wrong)}")

    # Rune Deck (CR 103.4)
    n_runes = sum(runes.values())
    if n_runes != 12:
        problems["runes"].append(f"Rune Deck has {n_runes} cards; it must be exactly 12 ({_delta(n_runes, 12)}).")
    not_rune = [n for n in runes if get(n) and "Rune" not in get(n)["types"]]
    if not_rune:
        problems["runes"].append(f"Not Rune cards: {_fmt_list(not_rune)}")
    if legend:
        off = [n for n in runes if get(n) and "Rune" in get(n)["types"] and not card_domains(get(n)) <= identity]
        if off:
            problems["runes"].append(f"Runes outside {'/'.join(sorted(identity))}: {_fmt_list(off)}")

    # Battlefields (CR 487.4.a: each player provides 3 in 1v1; CR 103.5.c: different names)
    if len(bfs) != 3:
        problems["battlefields"].append(f"{len(bfs)} Battlefields; 1v1 play needs exactly 3.")
    dupes = [n for n, k in Counter(bfs).items() if k > 1]
    if dupes:
        problems["battlefields"].append(f"Repeated Battlefields: {_fmt_list(dupes)}")
    not_bf = [n for n in bfs if get(n) and "Battlefield" not in get(n)["types"]]
    if not_bf:
        problems["battlefields"].append(f"Not Battlefield cards: {_fmt_list(not_bf)}")

    if fmt == "standard":
        # Sideboard (TR 601.1.c.1-2)
        if sum(side.values()) > 10:
            problems["sideboard"].append(f"Sideboard has {sum(side.values())} cards; the limit is 10.")
        bad_side = [
            n for n in side
            if get(n) and (not set(get(n)["types"]) & set(MAIN_DECK_TYPES) or "Token" in get(n).get("supertypes", [])
                           or ("Signature" in get(n).get("supertypes", []) and get(n).get("champion_tag") != champ_tag))
        ]
        if bad_side:
            problems["sideboard"].append(f"Not valid Main Deck cards for this deck: {_fmt_list(bad_side)}")

        # Banned list (TR 601.2.d) and set legality (TR 601.2.a, 601.3.c)
        everything = {deck.get("legend"), deck.get("champion"), *main, *side, *runes, *bfs} - {None}
        banned = [n for n in everything if get(n) and catalog.is_banned(get(n), "standard")]
        if banned:
            problems["banned"].append(f"Banned in Standard Constructed (Rules Hub list of {catalog.legality.updated}): {_fmt_list(banned)}")
        legal_sets = set(catalog.legality.standard_sets)
        if legal_sets:
            off_set = [n for n in everything if get(n) and not catalog.card_sets(get(n)) & legal_sets]
            if off_set:
                problems["sets"].append(f"Not from a Standard set ({', '.join(sorted(legal_sets))}): {_fmt_list(off_set)}")
        else:
            warnings.append("Could not read the Standard set list from TR 601.3.c, so set legality was not checked.")
        for f, names in catalog.legality.unmatched.items():
            warnings.append(f"Banned-list names that match no card ({f}): {', '.join(names)}")

    checker = CitationChecker(catalog.kb, Retrieval("deck construction"), {})
    checks = []
    for cid, (title, refs) in CHECKS.items():
        if fmt != "standard" and cid in STANDARD_ONLY:
            continue
        if fmt != "standard":
            refs = [r for r in refs if not r.startswith("TR")]
        cites = [checker.resolve(r) for r in refs]
        checks.append({
            "id": cid,
            "title": title,
            "ok": not problems[cid],
            "problems": problems[cid],
            "rules": [{"ref": c.ref, "verified": c.verified, "link": c.link} for c in cites],
        })
    failed = [c for c in checks if not c["ok"]]
    return {
        "deck": deck.get("name"),
        "format": fmt,
        "legal": not failed,
        "summary": f"{len(checks) - len(failed)}/{len(checks)} checks passed" + ("" if not failed else f"; failed: {', '.join(c['id'] for c in failed)}"),
        "checks": checks,
        "warnings": warnings,
    }


def compact_report(report: dict) -> dict:
    """What the model sees: failures in full, passes by id only."""
    return {
        "deck": report["deck"],
        "format": report["format"],
        "legal": report["legal"],
        "summary": report["summary"],
        "failed": [
            {"check": c["id"], "rules": [r["ref"] for r in c["rules"]], "problems": c["problems"]}
            for c in report["checks"] if not c["ok"]
        ],
        "passed": [c["id"] for c in report["checks"] if c["ok"]],
        "warnings": report["warnings"],
    }
