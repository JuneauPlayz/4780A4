"""Parsing and validating LLM output before anything runs."""

import pytest

from agent.sandbox import Sandbox, UnsafeNameError, deck_slug
from agent.validator import PlanError, parse_plan
from tests.conftest import plan


def errors_for(action, args):
    return parse_plan(plan((action, args))).errors


def test_valid_plan_from_code_fenced_text():
    raw = '```json\n{"thought": "t", "actions": [{"action": "list_decks", "args": {}}], "final_answer": null}\n```'
    p = parse_plan(raw)
    assert p.valid and p.actions[0].name == "list_decks"


@pytest.mark.parametrize("raw", ["not json at all", "{\"thought\": ", "[1, 2, 3]"])
def test_malformed_json_is_rejected(raw):
    with pytest.raises(PlanError):
        parse_plan(raw)


def test_wrong_top_level_types():
    with pytest.raises(PlanError, match="'actions' must be a list"):
        parse_plan({"thought": "t", "actions": "list_decks"})
    with pytest.raises(PlanError, match="final_answer"):
        parse_plan({"thought": "t", "actions": [], "final_answer": 42})


def test_empty_plan_is_rejected():
    with pytest.raises(PlanError, match="no actions"):
        parse_plan({"thought": "t", "actions": [], "final_answer": None})


def test_unknown_action_is_not_allowed():
    errs = errors_for("run_shell", {"cmd": "rm -rf /"})
    assert errs and "not an allowed action" in errs[0]


def test_action_items_must_have_shape():
    p = parse_plan({"thought": "t", "actions": ["list_decks", {"action": "list_decks", "args": {}, "deck": "x"}]})
    assert "must be" in p.errors[0]
    assert "put parameters inside 'args'" in p.errors[1]


def test_unknown_and_missing_arguments():
    errs = errors_for("log_match", {"deck": "jinx", "opponent": "Ahri", "winner": "me"})
    assert any("unknown argument" in e for e in errs)
    assert any("missing required argument 'result'" in e for e in errs)


@pytest.mark.parametrize(
    "args, message",
    [
        ({"deck": "jinx", "opponent": "Ahri", "result": "victory"}, "must be one of"),
        ({"deck": "jinx", "opponent": 7, "result": "win"}, "must be a string"),
        ({"deck": "jinx", "opponent": "x" * 81, "result": "win"}, "longer than"),
    ],
)
def test_bad_argument_values(args, message):
    assert any(message in e for e in errors_for("log_match", args))


def test_int_and_enum_normalization():
    p = parse_plan(plan(("search_cards", {"max_energy": "3", "types": ["unit", "SPELL"], "domain": "fury"})))
    assert p.valid
    assert p.actions[0].args["max_energy"] == 3
    assert p.actions[0].args["types"] == ["Unit", "Spell"]
    assert p.actions[0].args["domain"] == "Fury"
    assert p.actions[0].args["limit"] == 20  # default applied
    assert any("between" in e for e in errors_for("search_cards", {"limit": 500}))
    assert any("integer" in e for e in errors_for("search_cards", {"max_energy": True}))


@pytest.mark.parametrize("name", ["../x", "../../etc/passwd", "/etc/passwd", "a/b", "a\\b", ".hidden", "~root", "x" * 41, "bad*name", ""])
def test_path_traversal_and_bad_deck_names_rejected(name):
    errs = errors_for("delete_deck", {"name": name})
    assert errs, name


def test_deck_names_are_normalized():
    assert deck_slug("Jinx Aggro") == "jinx-aggro"
    assert deck_slug("  OLD_draven ") == "old-draven"
    p = parse_plan(plan(("load_deck", {"name": "Jinx Aggro"})))
    assert p.actions[0].args["name"] == "jinx-aggro"


def test_sandbox_paths_stay_inside(tmp_path):
    sb = Sandbox(tmp_path / "s")
    assert sb.deck_path("jinx").parent == sb.root / "decks"
    with pytest.raises(UnsafeNameError):
        sb.path("decks", "../../outside.json")
    with pytest.raises(UnsafeNameError):
        sb.deck_path("../x")


def test_card_lists_accept_two_shapes_and_check_counts():
    base = {"name": "d", "legend": "L", "champion": "C", "runes": [{"card": "Fury Rune", "count": 12}], "battlefields": ["a", "b", "c"]}
    p = parse_plan(plan(("save_deck", {**base, "main": {"Jinx, Rebel": 3}})))
    assert p.valid and p.actions[0].args["main"] == [{"card": "Jinx, Rebel", "count": 3}]
    errs = parse_plan(plan(("save_deck", {**base, "main": [{"card": "Jinx, Rebel", "count": 0}]}))).errors
    assert any("count must be an integer from 1 to 12" in e for e in errs)
    errs = parse_plan(plan(("save_deck", {**base, "main": ["Jinx, Rebel"]}))).errors
    assert any("must be an object" in e for e in errs)


def test_too_many_actions_in_one_plan():
    with pytest.raises(PlanError, match="too many actions"):
        parse_plan(plan(*[("list_decks", {})] * 9))


def test_final_answer_with_actions_is_ignored():
    p = parse_plan(plan(("list_decks", {}), final="All done!"))
    assert p.final_answer is None and any("final_answer ignored" in n for n in p.notes)


def test_ask_user_shape():
    p = parse_plan(plan(ask={"question": "Which deck?", "options": ["a", "b"]}))
    assert p.ask_user == {"question": "Which deck?", "options": ["a", "b"]}
    with pytest.raises(PlanError):
        parse_plan(plan(ask={"options": ["a"]}))
