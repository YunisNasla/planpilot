"""Drives the real Streamlit page with Claude faked. Run with: python -m pytest -q"""
import json
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import planner


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)  # audit log lands in tmp
    replies = []
    monkeypatch.setattr(planner, "call_claude", lambda client, system, messages, log: replies.pop(0))
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=10).run()
    assert not at.exception
    return at, replies


def plan(reply, *actions):
    return json.dumps({"reply": reply, "needs_clarification": False,
                       "actions": [{"action": a, "args": args} for a, args in actions]})


def button(at, label):
    return next(b for b in at.button if b.label == label)


def chat_text(at):
    return " ".join(m.markdown[i].value for m in at.chat_message for i in range(len(m.markdown)))


def test_safe_action_runs_immediately(app):
    at, replies = app
    store = at.session_state["store"]
    before = len(store.list_tasks())
    replies.append(plan("Added it.", ("add_task", {"title": "Buy milk", "due_date": "2030-01-01"})))
    at.chat_input[0].set_value("add buy milk").run()
    assert not at.exception
    assert len(store.list_tasks()) == before + 1
    assert "✅" in chat_text(at)


def test_delete_waits_for_confirmation(app):
    at, replies = app
    store = at.session_state["store"]
    replies.append(plan("Deleting the parking permit task.", ("delete_task", {"task_id": 4})))
    at.chat_input[0].set_value("delete parking permit").run()
    assert at.session_state["pending"] is not None
    assert any(t["id"] == 4 for t in store.list_tasks())  # not deleted yet
    button(at, "Confirm and run").click().run()
    assert not at.exception
    assert not any(t["id"] == 4 for t in store.list_tasks())
    kinds = [e["kind"] for e in at.session_state["log"].entries]
    assert "awaiting_confirmation" in kinds and "confirmed" in kinds


def test_cancel_changes_nothing(app):
    at, replies = app
    store = at.session_state["store"]
    before = store.list_events()
    replies.append(plan("Deleting the dentist appointment.", ("delete_event", {"event_id": 2})))
    at.chat_input[0].set_value("delete dentist").run()
    button(at, "Cancel").click().run()
    assert store.list_events() == before
    assert "Cancelled" in chat_text(at)


def test_invalid_plan_is_blocked(app):
    at, replies = app
    store = at.session_state["store"]
    before = store.list_tasks()
    bad = plan("Done.", ("drop_table", {}))
    replies += [bad, bad]  # first try + self-correction attempt
    at.chat_input[0].set_value("do something weird").run()
    assert store.list_tasks() == before
    assert at.error and "nothing was changed" in at.error[0].value


def test_api_failure_shows_friendly_error(app, monkeypatch):
    at, _ = app

    def boom(*a):
        raise planner.LLMError("Claude is busy or unreachable right now. Please try again in a minute.")

    monkeypatch.setattr(planner, "call_claude", boom)
    at.chat_input[0].set_value("hi").run()
    assert not at.exception
    assert "busy" in at.error[0].value


def test_user_pasted_key_is_used(app, monkeypatch):
    at, replies = app
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    at.chat_input[0].set_value("add buy milk").run()
    assert "No API key set" in at.error[0].value
    at.session_state["user_key"] = "pasted-key"
    replies.append(plan("Added it.", ("add_task", {"title": "Buy milk", "due_date": "2030-01-01"})))
    at.chat_input[0].set_value("add buy milk").run()
    assert not at.exception
    assert "✅" in chat_text(at)


def test_key_dialog_saves_and_clears(app):
    at, _ = app
    button(at, "Set API key").click().run()
    at.text_input[0].input("  sk-ant-test  ")
    button(at, "Save").click().run()
    assert not at.exception
    assert at.session_state["user_key"] == "sk-ant-test"
    button(at, "Change API key").click().run()
    button(at, "Clear").click().run()
    assert "user_key" not in at.session_state
