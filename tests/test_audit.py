"""Audit log: one JSONL line per event with the fields an auditor needs."""

import json

import pytest

from agent.audit import AuditLog
from agent.loop import Agent
from tests.conftest import ScriptedLLM, plan

FIELDS = {"ts", "session", "request", "event", "action", "args", "tier", "outcome", "error"}


def test_every_line_is_json_with_required_fields(session):
    llm = ScriptedLLM([
        plan(("log_match", {"deck": "jinx-starter", "opponent": "Ahri", "result": "win"}), ("delete_deck", {"name": "old-draven"})),
        plan(final="Done."),
    ])
    agent = Agent(session, llm)
    agent.resume(agent.submit("Log a win, then delete old-draven"), confirm=True)
    lines = session.sandbox.audit_path.read_text().splitlines()
    entries = [json.loads(l) for l in lines]
    assert len(entries) == len(lines) > 5
    assert all(FIELDS <= e.keys() and e["session"] == "test" for e in entries)
    by_action = {(e["event"], e["action"]) for e in entries}
    for ev in ("proposed", "validated", "executed"):
        assert (ev, "log_match") in by_action
    for ev in ("proposed", "validated", "awaiting_confirmation", "confirmed", "executed"):
        assert (ev, "delete_deck") in by_action
    executed = next(e for e in entries if e["event"] == "executed" and e["action"] == "log_match")
    assert executed["tier"] == "write" and executed["outcome"] == "done" and executed["args"]["opponent"] == "Ahri"
    assert executed["changes"]


def test_rejected_and_failed_actions_are_logged(session):
    llm = ScriptedLLM([plan(("drop_tables", {})), plan(("load_deck", {"name": "nope"})), plan(final="No such deck.")])
    Agent(session, llm).submit("x")
    entries = session.audit.read()
    rejected = next(e for e in entries if e["event"] == "rejected")
    assert rejected["action"] == "drop_tables" and "not an allowed action" in rejected["error"]
    failed = next(e for e in entries if e["event"] == "failed")
    assert failed["action"] == "load_deck" and "No saved deck" in failed["error"]


def test_long_args_are_truncated_and_unknown_events_refused(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", "s")
    entry = log.log("proposed", action="x", args={"blob": "y" * 10000})
    assert entry["args"]["truncated"] is True
    with pytest.raises(ValueError):
        log.log("made_up_event")
