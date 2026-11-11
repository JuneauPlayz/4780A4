"""Shared fixtures. No test needs an API key or the network: the LLM is a scripted stub."""

from __future__ import annotations

import json

import pytest

from agent.session import RULES, AgentSession
from riftbound.llm import LLMResult


@pytest.fixture(scope="session")
def catalog():
    return RULES.catalog


@pytest.fixture
def session(tmp_path):
    s = AgentSession.create(root=tmp_path, session_id="test")
    yield s
    s.close()


class ScriptedLLM:
    """Returns the scripted replies in order. A reply can be a dict, raw text, or an exception."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str) -> LLMResult:
        self.prompts.append((system, user))
        if not self.replies:
            raise AssertionError("the stub LLM ran out of scripted replies")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, str):
            return LLMResult(None, r, "stub", "stub", 0.0)
        return LLMResult(r, json.dumps(r), "stub", "stub", 0.0)


def plan(*actions, final=None, ask=None, thought="Refined request: test."):
    return {
        "thought": thought,
        "actions": [{"action": a, "args": args} for a, args in actions],
        "final_answer": final,
        "ask_user": ask,
    }
