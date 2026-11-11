"""Deterministic deck validation against known legal and illegal decks."""

import copy
import json
from pathlib import Path

import pytest

from agent.deckrules import CHECKS, validate_deck

SEED = Path(__file__).resolve().parent.parent / "agent" / "seed"


@pytest.fixture
def jinx():
    return json.loads((SEED / "jinx-starter.json").read_text(encoding="utf-8"))


def failed(report):
    return {c["id"] for c in report["checks"] if not c["ok"]}


def problems(report, check):
    return " ".join(next(c for c in report["checks"] if c["id"] == check)["problems"])


def test_every_cited_rule_exists_in_the_index(catalog):
    for title, refs in CHECKS.values():
        for ref in refs:
            kind = "core_rules" if ref.startswith("CR") else "tournament_rules"
            assert catalog.kb.rule_exists(kind, ref.split()[1]), f"{title}: {ref}"


def test_seed_jinx_deck_is_legal(catalog, jinx):
    report = validate_deck(jinx, catalog)
    assert report["legal"], report["summary"]
    assert all(r["verified"] and r["link"] for c in report["checks"] for r in c["rules"])


def test_seed_draven_deck_fails_only_banned(catalog):
    deck = json.loads((SEED / "old-draven.json").read_text(encoding="utf-8"))
    report = validate_deck(deck, catalog)
    assert failed(report) == {"banned"}
    for name in ("Draven, Vanquisher", "Reaver's Row", "Scrapheap", "Stacked Deck"):
        assert name in problems(report, "banned")


def mutate(deck, fn):
    d = copy.deepcopy(deck)
    fn(d)
    return d


@pytest.mark.parametrize(
    "change, check, text",
    [
        (lambda d: d["main"].update({"Cleave": 2}), "main_size", "39 cards; Standard requires exactly 40 (add 1)"),
        (lambda d: d["main"].update({"Cleave": 4, "Hextech Ray": 2}), "copies", "Cleave x4"),
        (lambda d: d.update(champion="Draven, Showboat"), "champion", "needs Jinx"),
        (lambda d: d.update(champion="Chemtech Enforcer"), "champion", "not a Champion unit"),
        (lambda d: d.update(legend="Jinx, Rebel"), "legend", "not a Legend"),
        (lambda d: (d["main"].pop("Cleave"), d["main"].update({"Fox-Fire, Ahri": 3})), "domain_identity", "Fox-Fire"),
        (lambda d: (d["main"].pop("Cleave"), d["main"].update({"Spinning Axe": 3})), "signature", "different champion"),
        (lambda d: (d["main"].pop("Treasure Trove"), d["main"].update({"Super Mega Death Rocket!, Jinx": 4})), "signature", "4 Signature"),
        (lambda d: (d["main"].pop("Cleave"), d["main"].update({"Forgefire Cape": 3})), "unique", "Forgefire Cape x3"),
        (lambda d: d["runes"].update({"Chaos Rune": 5}), "runes", "11 cards; it must be exactly 12 (add 1)"),
        (lambda d: d["runes"].update({"Chaos Rune": 3, "Mind Rune": 3}), "runes", "Mind Rune"),
        (lambda d: d["battlefields"].pop(), "battlefields", "2 Battlefields"),
        (lambda d: d.update(battlefields=["Back-Alley Bar"] * 3), "battlefields", "Repeated"),
        (lambda d: (d["main"].pop("Cleave"), d["main"].update({"Gold": 3})), "main_types", "token"),
        (lambda d: (d["main"].pop("Cleave"), d["main"].update({"Fury Rune": 3})), "main_types", "Rune"),
        (lambda d: d.update(sideboard={"Hextech Ray": 1}), "copies", "Hextech Ray x4"),
        (lambda d: d.update(sideboard={n: 1 for n in ["Gust", "Shakedown", "Rebuke", "Ride the Wind", "Morbid Return", "Acceptable Losses",
                                                    "Invert Timelines", "Blind Fury", "Whirlwind", "Thermo Beam", "Disintegrate"]}),
         "sideboard", "11 cards"),
        (lambda d: (d["main"].pop("Cleave"), d["main"].update({"Called Shot": 3})), "banned", "Called Shot"),
    ],
)
def test_illegal_decks_are_caught_with_the_right_check(catalog, jinx, change, check, text):
    report = validate_deck(mutate(jinx, change), catalog)
    assert not report["legal"]
    assert check in failed(report), report["summary"]
    assert text in problems(report, check)


def test_casual_format_skips_tournament_rules(catalog, jinx):
    deck = mutate(jinx, lambda d: (d["main"].update({"Called Shot": 1})))  # 41 cards, one banned
    assert {"main_size", "banned"} <= failed(validate_deck(deck, catalog, "standard"))
    casual = validate_deck(deck, catalog, "casual")
    assert casual["legal"], casual["summary"]
    assert not any(r["ref"].startswith("TR") for c in casual["checks"] for r in c["rules"])


def test_unknown_format_is_an_error(catalog, jinx):
    with pytest.raises(ValueError):
        validate_deck(jinx, catalog, "limited")
