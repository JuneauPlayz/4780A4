"""The ReAct loop with a scripted stub LLM: confirmation gating, validation feedback, limits."""

import pytest

from agent.loop import Agent
from riftbound import config
from riftbound.llm import LLMError
from tests.conftest import ScriptedLLM, plan


def events(session, request_id=None):
    return [e["event"] for e in session.audit.read() if request_id is None or e["request"] == request_id]


def test_multi_step_request_reaches_final_answer(session):
    llm = ScriptedLLM([
        plan(("list_decks", {})),
        plan(("log_match", {"deck": "jinx-starter", "opponent": "Ahri", "result": "win"})),
        plan(final="Logged a win for jinx-starter vs Ahri."),
    ])
    turn = Agent(session, llm).submit("Log a win with jinx-starter against Ahri")
    assert turn.status == "done" and turn.final_answer.startswith("Logged a win")
    assert [a.status for a in turn.actions] == ["done", "done"]
    # Observations from step 1 are in the prompt for step 2 (ReAct).
    assert '"action": "list_decks", "status": "done"' in llm.prompts[1][1]


def test_destructive_action_pauses_and_cancel_changes_nothing(session):
    llm = ScriptedLLM([
        plan(("delete_deck", {"name": "old-draven"}), ("list_decks", {})),
        plan(final="You cancelled, so old-draven was kept."),
    ])
    agent = Agent(session, llm)
    turn = agent.submit("Delete old-draven")
    assert turn.status == "awaiting_confirmation" and turn.pending.name == "delete_deck"
    assert len(llm.prompts) == 1  # the loop is paused, not still calling the model
    turn = agent.resume(turn, confirm=False)
    assert session.decks.exists("old-draven")
    assert [a.status for a in turn.actions] == ["cancelled", "skipped"]
    assert turn.status == "done"
    ev = events(session, turn.id)
    assert "awaiting_confirmation" in ev and "cancelled" in ev and "executed" not in ev[ev.index("cancelled"):]


def test_cancelled_action_cannot_be_proposed_again(session):
    llm = ScriptedLLM([
        plan(("delete_deck", {"name": "old-draven"})),
        plan(("delete_deck", {"name": "old-draven"})),  # the model tries again after the player said no
        plan(final="Kept old-draven."),
    ])
    agent = Agent(session, llm)
    turn = agent.resume(agent.submit("Delete old-draven"), confirm=False)
    assert turn.status == "done" and session.decks.exists("old-draven")
    assert [a.status for a in turn.actions] == ["cancelled", "blocked"]
    assert "cancelled this action earlier" in turn.actions[1].error


def test_confirmed_destructive_action_runs(session):
    llm = ScriptedLLM([plan(("delete_deck", {"name": "old-draven"})), plan(final="Deleted.")])
    agent = Agent(session, llm)
    turn = agent.resume(agent.submit("Delete old-draven"), confirm=True)
    assert turn.status == "done" and not session.decks.exists("old-draven")
    ev = events(session, turn.id)
    assert ev.index("awaiting_confirmation") < ev.index("confirmed") < ev.index("executed", ev.index("confirmed"))


def test_invalid_plan_gets_one_retry_with_feedback(session):
    llm = ScriptedLLM([
        plan(("delete_deck", {"name": "../../etc"})),
        plan(("list_decks", {})),
        plan(final="Here are your decks."),
    ])
    turn = Agent(session, llm).submit("Show my decks")
    assert turn.status == "done"
    assert turn.steps[0].rejected and turn.steps[0].actions[0].status == "rejected"
    assert "REJECTED BEFORE ANYTHING RAN" in llm.prompts[1][1] and "path characters" in llm.prompts[1][1]
    assert "plan_rejected" in events(session, turn.id)


def test_two_invalid_plans_fail_and_nothing_runs(session):
    llm = ScriptedLLM([
        plan(("delete_deck", {"name": "old-draven"}), ("format_disk", {})),
        "this is not json",
    ])
    turn = Agent(session, llm).submit("Delete everything")
    assert turn.status == "failed" and "failed validation twice" in turn.error
    assert session.decks.exists("old-draven")  # the valid action in a rejected plan never ran
    assert "executed" not in events(session, turn.id)


def test_failed_action_skips_rest_of_step(session):
    llm = ScriptedLLM([
        plan(("load_deck", {"name": "missing"}), ("delete_deck", {"name": "old-draven"})),
        plan(final="That deck does not exist."),
    ])
    turn = Agent(session, llm).submit("Load missing then delete old-draven")
    assert [a.status for a in turn.actions] == ["failed", "skipped"]
    assert turn.status == "done" and session.decks.exists("old-draven")


def test_llm_error_is_reported(session):
    turn = Agent(session, ScriptedLLM([LLMError("429 rate limit")])).submit("Hi")
    assert turn.status == "failed" and "429" in turn.error
    assert "llm_error" in events(session, turn.id)


def test_flipped_interaction_asks_the_user(session):
    llm = ScriptedLLM([plan(ask={"question": "Which Master Yi legend?", "options": ["Master Yi, Wuju Master", "Wuju Bladesman, Starter"]})])
    turn = Agent(session, llm).submit("Build a Master Yi deck")
    assert turn.status == "needs_input" and turn.ask_user["question"] == "Which Master Yi legend?"


def test_step_limit(session, monkeypatch):
    monkeypatch.setattr(config, "MAX_LLM_CALLS_PER_REQUEST", 2)
    llm = ScriptedLLM([plan(("list_decks", {})), plan(("list_decks", {}))])
    turn = Agent(session, llm).submit("Loop forever")
    assert turn.status == "limit" and "step limit" in turn.final_answer
    assert "last step" in llm.prompts[1][1].lower()


def test_action_limit(session, monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIONS_PER_REQUEST", 3)
    llm = ScriptedLLM([plan(*[("list_decks", {})] * 4)])
    turn = Agent(session, llm).submit("Many actions")
    assert turn.status == "limit" and "action limit" in turn.final_answer


def test_session_request_limit(session):
    session.request_limit = 1
    agent = Agent(session, ScriptedLLM([plan(final="hi")]))
    assert agent.submit("one").status == "done"
    second = agent.submit("two")
    assert second.status == "limit" and "requests" in second.error


def test_citations_in_final_answer_are_checked(session):
    llm = ScriptedLLM([
        plan(("lookup_rule", {"question": "copies per deck"})),
        plan(final="Up to 3 copies [CR 103.3.b]; see also [CR 999.9]."),
    ])
    turn = Agent(session, llm).submit("How many copies?")
    assert "[[CR 103.3.b]](" in turn.final_answer
    assert "⚠ CR 999.9 not found" in turn.final_answer
    assert any("CR 999.9" in w for w in turn.warnings)


def test_history_and_state_reach_the_next_request(session):
    llm = ScriptedLLM([plan(final="First answer."), plan(final="Second.")])
    agent = Agent(session, llm)
    agent.submit("first question")
    agent.submit("second question")
    user_prompt = llm.prompts[1][1]
    assert "first question" in user_prompt and "First answer." in user_prompt
    assert '"saved_decks"' in user_prompt and "jinx-starter" in user_prompt


@pytest.mark.parametrize("block", ["## Persona", "## Reason -> Act -> Observe", "## Output template", "## Allowed actions",
                                   "## Question Refinement", "## Flipped Interaction", "## Examples"])
def test_system_prompt_has_every_pattern_block(block):
    from agent.prompts import build_system_prompt

    assert block in build_system_prompt()


def test_action_catalog_lists_every_action():
    from agent.actions import REGISTRY
    from agent.prompts import action_catalog_block

    text = action_catalog_block()
    assert all(f"- {name}  [" in text for name in REGISTRY)
