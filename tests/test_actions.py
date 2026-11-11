"""Each action, run through the validator and executor exactly as the agent runs it."""

import json
import time

import pytest

from agent import actions
from agent.executor import Executor
from agent.validator import parse_plan
from riftbound import config
from tests.conftest import plan

JINX_ARGS = {
    "name": "jinx-copy",
    "legend": "Jinx, Loose Cannon",
    "champion": "jinx rebel",  # loose spelling is resolved to the official name
    "main": [
        {"card": "Jinx, Rebel", "count": 3}, {"card": "Super Mega Death Rocket!, Jinx", "count": 2},
        {"card": "Chemtech Enforcer", "count": 3}, {"card": "Legion Rearguard", "count": 3},
        {"card": "Flame Chompers", "count": 3}, {"card": "Raging Soul", "count": 3},
        {"card": "Scrapyard Champion", "count": 2}, {"card": "Undercover Agent", "count": 2},
        {"card": "Traveling Merchant", "count": 3}, {"card": "Get Excited!", "count": 3},
        {"card": "Hextech Ray", "count": 3}, {"card": "Void Seeker", "count": 3},
        {"card": "Falling Star", "count": 2}, {"card": "Treasure Trove", "count": 2}, {"card": "Cleave", "count": 3},
    ],
    "runes": [{"card": "Fury Rune", "count": 6}, {"card": "Chaos Rune", "count": 6}],
    "battlefields": ["Back-Alley Bar", "Navori Fighting Pit", "Reckoner's Arena"],
}


def run(session, action, args=None, confirmed=False):
    p = parse_plan(plan((action, args or {})))
    assert p.valid, p.errors
    ex = Executor(session)
    rec = ex.prepare(p.actions[0], "req", "req-1")
    return ex.execute(rec, "req", confirmed=confirmed)


# ---------------------------------------------------------------------- database


def test_search_cards_with_legend_filter(session):
    rec = run(session, "search_cards", {"legend": "Jinx, Loose Cannon", "limit": 40})
    assert rec.status == "done" and rec.tier == "read" and rec.domain == "database"
    cards = rec.data["cards"]
    assert cards and all(set(c["domains"]) <= {"Fury", "Chaos", "Colorless"} for c in cards)
    assert not any(c.get("banned") for c in cards)
    assert "Legend" not in {c["type"] for c in cards}


def test_search_flags_champions_with_several_legends(session):
    rec = run(session, "search_cards", {"types": ["Legend"], "tag": "Master Yi"})
    assert any("2 Legends belong to Master Yi" in n and "ask_user" in n for n in rec.data["notes"])
    one = run(session, "search_cards", {"types": ["Legend"], "tag": "Jinx"})
    assert not any("Legends belong" in n for n in one.data["notes"])


def test_legend_pool_has_only_main_deck_types(session):
    rec = run(session, "search_cards", {"legend": "Master Yi, Wuju Master", "limit": 40, "name": "H"})
    names = {c["name"] for c in rec.data["cards"]}
    assert "Hush" not in names and "Hunker Down" not in names  # no card type in the gallery data
    assert all(c["type"] in ("Unit", "Gear", "Spell") for c in rec.data["cards"])


def test_search_cards_can_include_banned(session):
    rec = run(session, "search_cards", {"name": "Called Shot", "include_banned": True})
    assert rec.data["cards"][0]["banned"] is True
    hidden = run(session, "search_cards", {"name": "Called Shot"})
    assert hidden.data["total_matches"] == 0 and "banned" in hidden.data["notes"][0]


def test_search_cards_sql_input_is_data(session):
    rec = run(session, "search_cards", {"name": "x' OR 1=1 --"})
    assert rec.status == "done" and rec.data["total_matches"] == 0


def test_get_card_and_ambiguity(session):
    rec = run(session, "get_card", {"name": "elnuk herd"})
    assert rec.data["name"] == "Elnuk Herd" and rec.data["errata"] is True
    amb = run(session, "get_card", {"name": "Ahri"})
    assert amb.status == "failed" and "Ahri, Alluring" in amb.error


def test_log_match_and_stats(session):
    rec = run(session, "log_match", {"deck": "jinx-starter", "opponent": "Ahri", "result": "win", "notes": "close game"})
    assert rec.status == "done" and rec.tier == "write"
    assert rec.changes and "matches: +1 row" in rec.changes[0]
    stats = run(session, "match_stats", {"deck": "jinx-starter"}).data
    assert stats["by_deck"][0]["wins"] == 3 and stats["games"] == 4  # 3 seeded + this one
    ahri = run(session, "match_stats", {"opponent": "ahri"}).data
    assert ahri["games"] == 3


# ---------------------------------------------------------------------- files


def test_save_new_deck_is_write_and_reports_changes(session):
    rec = run(session, "save_deck", JINX_ARGS)
    assert rec.status == "done" and rec.tier == "write"
    assert rec.data["quick_validation"]["legal"] is True
    assert rec.changes[0].startswith("new deck 'jinx-copy': 40 main")
    saved = json.loads(session.sandbox.deck_path("jinx-copy").read_text())
    assert saved["champion"] == "Jinx, Rebel"


def test_overwrite_is_destructive_with_diff_preview(session):
    run(session, "save_deck", JINX_ARGS)
    session.created_this_request = set()  # a later request: the deck now belongs to the player
    changed = {**JINX_ARGS, "main": JINX_ARGS["main"][:-1] + [{"card": "Hextech Ray", "count": 3}]}
    rec = run(session, "save_deck", changed)
    assert rec.tier == "destructive" and rec.status == "awaiting_confirmation"
    assert any("main: -3 Cleave" in line for line in rec.preview)
    assert json.loads(session.sandbox.deck_path("jinx-copy").read_text())["main"]["Cleave"] == 3  # unchanged until confirmed
    done = run(session, "save_deck", changed, confirmed=True)
    assert done.status == "done" and "Cleave" not in json.loads(session.sandbox.deck_path("jinx-copy").read_text())["main"]


def test_revising_a_draft_saved_in_the_same_request_is_not_destructive(session):
    run(session, "save_deck", JINX_ARGS)
    rec = run(session, "save_deck", {**JINX_ARGS, "notes": "fixed after validation"})
    assert rec.tier == "write" and rec.status == "done" and "notes changed" in rec.changes
    assert run(session, "save_deck", {**JINX_ARGS, "name": "jinx-starter"}).tier == "destructive"  # seeded deck


def test_save_with_unknown_card_fails_with_suggestions(session):
    bad = {**JINX_ARGS, "main": [{"card": "Jinxx, Rebel", "count": 3}, {"card": "Totally Fake Card", "count": 3}]}
    rec = run(session, "save_deck", bad)
    assert rec.status == "failed"
    assert "Did you mean: Jinx, Rebel" in rec.error and "Totally Fake Card" in rec.error
    assert not session.sandbox.deck_path("jinx-copy").exists()


def test_load_list_and_missing_deck(session):
    assert {d["name"] for d in run(session, "list_decks").data["decks"]} == {"jinx-starter", "old-draven"}
    assert run(session, "load_deck", {"name": "jinx-starter"}).data["legend"] == "Jinx, Loose Cannon"
    missing = run(session, "load_deck", {"name": "nope"})
    assert missing.status == "failed" and "Saved decks: jinx-starter, old-draven" in missing.error


def test_delete_requires_confirmation(session):
    rec = run(session, "delete_deck", {"name": "old-draven"})
    assert rec.status == "awaiting_confirmation" and "cannot be undone" in rec.preview[0]
    assert session.decks.exists("old-draven")
    rec = run(session, "delete_deck", {"name": "old-draven"}, confirmed=True)
    assert rec.status == "done" and not session.decks.exists("old-draven")


def test_delete_missing_deck_fails_before_confirmation(session):
    rec = run(session, "delete_deck", {"name": "ghost"})
    assert rec.status == "failed" and "No saved deck" in rec.error


@pytest.mark.parametrize("fmt, needle", [("text", "Main Deck (40)"), ("csv", "section,count,card"), ("json", '"legend"')])
def test_export_writes_into_sandbox(session, fmt, needle):
    rec = run(session, "export_deck", {"name": "jinx-starter", "format": fmt})
    assert rec.status == "done" and needle in rec.file["content"]
    path = session.sandbox.root / rec.data["file"]
    assert path.exists() and path.parent == session.sandbox.root / "exports"


# ---------------------------------------------------------------------- validation and rules


def test_validate_deck_action(session):
    rec = run(session, "validate_deck", {"name": "old-draven"})
    assert rec.domain == "validation" and "NOT LEGAL" in rec.summary
    assert rec.data["failed"][0]["check"] == "banned" and rec.data["failed"][0]["rules"] == ["TR 601.2.d"]
    assert rec.display["checks"]  # full report for the UI


def test_lookup_rule_returns_passages_and_records_them(session):
    rec = run(session, "lookup_rule", {"question": "How many copies of a card can a deck have?"})
    assert rec.status == "done" and rec.data["passages"]
    assert any("103.3.b" in p["text"] for p in rec.data["passages"])
    assert session.retrieved


# ---------------------------------------------------------------------- network


def test_check_rules_version_without_network(session, monkeypatch):
    import riftbound.sources as sources

    class Resp:
        text = "<html><body><h2>Core Rules</h2></body></html>"

    monkeypatch.setattr(sources, "fetch", lambda url, timeout=60: Resp())
    rec = run(session, "check_rules_version")
    assert rec.status == "done" and rec.domain == "network"
    assert rec.data["local_core_rules"] == session.kb.manifest["core_rules"]["last_updated"]
    again = run(session, "check_rules_version")
    assert again.status == "blocked" and "rate limited" in again.error


def test_check_rules_version_network_error(session, monkeypatch):
    import riftbound.sources as sources

    def boom(url, timeout=60):
        raise ConnectionError("offline")

    monkeypatch.setattr(sources, "fetch", boom)
    rec = run(session, "check_rules_version")
    assert rec.status == "failed" and "Could not reach" in rec.error


def test_refresh_is_destructive_and_rate_limited(session, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SANDBOX_ROOT", tmp_path)
    rec = run(session, "refresh_rules")
    assert rec.tier == "destructive" and rec.status == "awaiting_confirmation"
    (tmp_path / ".last_refresh").write_text(str(time.time()))
    assert run(session, "refresh_rules").status == "blocked"
    monkeypatch.setattr(config, "ALLOW_REFRESH", False)
    blocked = run(session, "refresh_rules")
    assert blocked.status == "blocked" and "disabled" in blocked.error


def test_registry_covers_at_least_three_domains():
    assert {s.domain for s in actions.REGISTRY.values()} >= {"database", "files", "validation", "rules", "network"}
    assert {s.tier for s in actions.REGISTRY.values()} == {"read", "write", "destructive"}


# ---------------------------------------------------------------------- edit_deck


def test_edit_deck_adds_and_removes_on_a_draft(session):
    short = {**JINX_ARGS, "main": JINX_ARGS["main"][:-1]}  # 37 cards: Cleave x3 left out
    run(session, "save_deck", short)
    rec = run(session, "edit_deck", {"name": "jinx-copy", "add": [{"card": "cleave", "count": 3}]})
    assert rec.tier == "write" and rec.status == "done"  # a draft from this request: no confirmation
    assert rec.data["main_count"] == 40 and rec.data["quick_validation"]["legal"] is True
    assert rec.changes == ["main: +3 Cleave (0 -> 3)"]
    rec = run(session, "edit_deck", {"name": "jinx-copy", "remove": [{"card": "Cleave", "count": 1}],
                                     "add": [{"card": "Hextech Ray", "count": 1}]})
    assert rec.status == "failed" and "Hextech Ray would have 4 copies" in rec.error  # refused, not saved


def test_edit_existing_deck_needs_confirmation(session):
    args = {"name": "old-draven", "remove": [{"card": "Draven, Vanquisher", "count": 3}],
            "add": [{"card": "Draven, Showboat", "count": 3}]}
    rec = run(session, "edit_deck", args)
    assert rec.tier == "destructive" and rec.status == "awaiting_confirmation"
    assert "main: -3 Draven, Vanquisher (3 -> 0)" in rec.preview
    assert session.decks.load("old-draven")["main"]["Draven, Vanquisher"] == 3
    assert run(session, "edit_deck", args, confirmed=True).status == "done"
    assert "Draven, Vanquisher" not in session.decks.load("old-draven")["main"]


def test_edit_deck_battlefields_and_errors(session):
    session.created_this_request.add("old-draven")
    rec = run(session, "edit_deck", {"name": "old-draven", "section": "battlefields",
                                     "remove": [{"card": "Reaver's Row", "count": 1}], "add": [{"card": "Reckoner's Arena", "count": 1}]})
    assert rec.status == "done" and "battlefields: +Reckoner's Arena" in rec.changes
    too_many = run(session, "edit_deck", {"name": "old-draven", "remove": [{"card": "Cleave", "count": 5}]})
    assert too_many.status == "failed" and "has 3 Cleave, so 5 cannot be removed" in too_many.error
    typo = run(session, "edit_deck", {"name": "old-draven", "add": [{"card": "Hextek Ray", "count": 1}]})
    assert typo.status == "failed" and "Did you mean: Hextech Ray" in typo.error
    empty = run(session, "edit_deck", {"name": "old-draven"})
    assert empty.status == "failed" and "needs at least one card" in empty.error
    assert session.decks.load("old-draven")["main"]["Cleave"] == 3  # failed edits change nothing


def test_edit_deck_refuses_to_break_the_copy_limit(session):
    session.created_this_request.add("jinx-starter")
    rec = run(session, "edit_deck", {"name": "jinx-starter", "remove": [{"card": "Treasure Trove", "count": 2}],
                                     "add": [{"card": "Hextech Ray", "count": 2}]})  # already has 3
    assert rec.status == "failed"
    assert "Hextech Ray would have 5 copies; the limit is 3" in rec.error
    assert "room for more copies: " in rec.error and "Falling Star" in rec.error  # 2 copies, so it has room
    assert session.decks.load("jinx-starter")["main"]["Treasure Trove"] == 2  # nothing changed
