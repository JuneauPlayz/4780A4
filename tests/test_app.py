"""Headless Streamlit test of the UI with a scripted stub LLM (no API key, no network)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import agent.loop
from riftbound import config
from tests.conftest import ScriptedLLM, plan

APP = Path(__file__).resolve().parent.parent / "app.py"


@pytest.fixture
def app(monkeypatch, tmp_path):
    def start(replies):
        monkeypatch.setenv("GEMINI_API_KEY", "stub-key-for-tests")
        monkeypatch.setattr(config, "SANDBOX_ROOT", tmp_path)
        monkeypatch.setattr(agent.loop, "generate_json", ScriptedLLM(replies))
        at = AppTest.from_file(str(APP), default_timeout=60)
        at.run()
        assert not at.exception, at.exception
        return at

    return start


def texts(at):
    return " ".join(m.value for m in at.markdown)


def test_app_loads_with_banner_tabs_and_demo_data(app):
    at = app([])
    assert "Rules data" in at.info[0].value and "Core Rules updated" in at.info[0].value
    assert [t.label for t in at.tabs][:4] == ["💬 Agent", "🗂️ Saved decks", "📊 Match stats", "🧾 Audit log"]
    assert at.selectbox[0].options == ["jinx-starter", "old-draven"]
    assert "Legal" in texts(at)


def test_confirm_flow_in_the_ui(app):
    at = app([
        plan(("validate_deck", {"name": "old-draven"}), ("delete_deck", {"name": "old-draven"})),
        plan(final="old-draven had banned cards [TR 601.2.d], and you confirmed the delete."),
    ])
    at.chat_input[0].set_value("Which decks have banned cards? Delete old-draven.").run()
    assert not at.exception, at.exception
    assert "waiting for confirmation" in texts(at)
    assert "cannot be undone" in texts(at)
    confirm = next(b for b in at.button if b.label == "Confirm")
    confirm.click().run()
    assert not at.exception, at.exception
    page = texts(at)
    assert "you confirmed the delete" in page and "decks: -old-draven.json" in page
    assert at.selectbox[0].options == ["jinx-starter"]


def test_cancel_flow_in_the_ui(app):
    at = app([plan(("delete_deck", {"name": "old-draven"})), plan(final="Kept old-draven.")])
    at.chat_input[0].set_value("Delete old-draven").run()
    next(b for b in at.button if b.label == "Cancel").click().run()
    assert not at.exception, at.exception
    assert "Kept old-draven." in texts(at) and "cancelled" in texts(at)
    assert "old-draven" in at.selectbox[0].options


def test_rejected_plan_is_shown(app):
    at = app([plan(("delete_deck", {"name": "../secrets"})), plan(final="I can only use saved deck names.")])
    at.chat_input[0].set_value("Delete ../secrets").run()
    assert not at.exception, at.exception
    assert any("Plan rejected by the validator" in e.value for e in at.error)
    assert "I can only use saved deck names." in texts(at)
