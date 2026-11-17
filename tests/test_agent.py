"""Run with: python -m pytest -q   (no API key needed; Claude is faked)."""
import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import anthropic
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import planner
from actions import AuditLog, execute, validate_plan
from store import Store, seed_demo

NOW = datetime(2026, 11, 8, 18, 40, tzinfo=ZoneInfo("America/Los_Angeles"))


@pytest.fixture
def store():
    s = Store()
    seed_demo(s, NOW.date())
    return s


@pytest.fixture
def log(tmp_path):
    return AuditLog("test", path=str(tmp_path / "audit.jsonl"))


def plan_of(*actions, reply="ok", clarify=False):
    return {"reply": reply, "needs_clarification": clarify,
            "actions": [{"action": a, "args": args} for a, args in actions]}


# ---------- store ----------

def test_store_crud(store):
    t = store.add_task("Test", "2026-11-20", "high")
    assert store.update_task(t["id"], title="Renamed")["title"] == "Renamed"
    assert store.complete_task(t["id"])["status"] == "done"
    store.delete_task(t["id"])
    with pytest.raises(KeyError):
        store.get_task(t["id"])


def test_event_default_end_and_move_keeps_length(store):
    e = store.add_event("Call", "2026-11-12T14:00")
    assert e["end"] == "2026-11-12T15:00"
    moved = store.update_event(e["id"], start="2026-11-13T09:30")
    assert (moved["start"], moved["end"]) == ("2026-11-13T09:30", "2026-11-13T10:30")


def test_store_rejects_non_whitelisted_columns(store):
    with pytest.raises(ValueError):
        store.update_task(1, **{"id = 99; --": "x"})


# ---------- validation ----------

def test_valid_plan_passes(store):
    p = validate_plan(plan_of(("add_task", {"title": "Call mom", "due_date": "2026-11-09"})), store)
    assert p["errors"] == [] and len(p["actions"]) == 1 and not p["actions"][0]["destructive"]


@pytest.mark.parametrize("action,args,expected", [
    ("format_disk", {}, "unknown action"),
    ("add_task", {}, "missing title"),
    ("add_task", {"title": "x", "color": "red"}, "unknown argument"),
    ("add_task", {"title": "x", "due_date": "next friday"}, "due_date must look like"),
    ("add_task", {"title": "x", "priority": "urgent"}, "priority must be one of"),
    ("complete_task", {"task_id": 999}, "does not exist"),
    ("complete_task", {"task_id": "3"}, "must be an integer"),
    ("update_task", {"task_id": 1}, "nothing to change"),
    ("add_event", {"title": "x", "start": "2026-11-10T10:00", "end": "2026-11-10T09:00"}, "must be after start"),
])
def test_bad_actions_rejected(store, action, args, expected):
    p = validate_plan(plan_of((action, args)), store)
    assert any(expected in e for e in p["errors"]), p["errors"]
    assert p["actions"] == []


def test_clarification_must_have_no_actions(store):
    p = validate_plan(plan_of(("complete_task", {"task_id": 1}), clarify=True), store)
    assert p["errors"]


def test_destructive_flag_and_conflict_warning(store):
    dentist = next(e for e in store.list_events() if e["title"].startswith("Dentist"))
    p = validate_plan(plan_of(
        ("delete_task", {"task_id": 1}),
        ("add_event", {"title": "Coffee", "start": dentist["start"]}),
    ), store)
    assert p["errors"] == []
    assert p["actions"][0]["destructive"] and not p["actions"][1]["destructive"]
    assert any("overlaps" in w for w in p["warnings"])


def test_too_many_actions(store):
    p = validate_plan(plan_of(*[("add_task", {"title": f"t{i}"}) for i in range(30)]), store)
    assert any("Too many" in e for e in p["errors"])


# ---------- execution + audit log ----------

def test_execute_runs_and_logs(store, log):
    p = validate_plan(plan_of(("add_task", {"title": "New"}), ("complete_task", {"task_id": 1})), store)
    results = execute(p["actions"], store, log)
    assert all(r["ok"] for r in results)
    lines = [json.loads(l) for l in Path(log.path).read_text().splitlines()]
    assert [l["action"] for l in lines] == ["add_task", "complete_task"]


def test_execute_reports_failure_and_continues(store, log):
    acts = [{"action": "delete_task", "args": {"task_id": 1}, "destructive": True}] * 2
    results = execute(acts, store, log)
    assert [r["ok"] for r in results] == [True, False]


# ---------- planner (Claude faked) ----------

def test_parse_json_handles_fences():
    assert planner.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert planner.parse_json('Sure! {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(ValueError):
        planner.parse_json("no json here")


class FakeClient:
    """Returns queued responses; an Exception in the queue is raised instead."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=item)],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def api_error(cls, status, headers=None):
    # Built without __init__ so the test doesn't depend on which HTTP library the SDK version uses.
    err = cls.__new__(cls)
    err.status_code = status
    err.response = SimpleNamespace(headers=headers or {}, status_code=status)
    return err


def test_prompt_contains_state_and_dates(store):
    system = planner.build_system_prompt(store, NOW)
    assert "Dentist appointment" in system and "Sun 2026-11-08" in system and "Fri 2026-11-13" in system


def test_self_correction_fixes_bad_plan(store, log):
    bad = json.dumps(plan_of(("complete_task", {"task_id": 999})))
    good = json.dumps(plan_of(("complete_task", {"task_id": 2})))
    client = FakeClient(bad, good)
    p = planner.make_plan("finish the lab report", [], store, client, log, NOW)
    assert p["errors"] == [] and p["self_corrected"]
    assert "rejected by the validator" in client.calls[1]["messages"][-1]["content"]


def test_gives_up_after_one_correction(store, log):
    client = FakeClient("not json", "still not json")
    p = planner.make_plan("hi", [], store, client, log, NOW)
    assert p["errors"] and len(client.calls) == 2


def test_retries_rate_limit_then_succeeds(store, log, monkeypatch):
    sleeps = []
    monkeypatch.setattr(planner.time, "sleep", sleeps.append)
    ok = json.dumps(plan_of(reply="Nothing to do."))
    client = FakeClient(api_error(anthropic.RateLimitError, 429, {"retry-after": "3"}),
                        api_error(anthropic.OverloadedError, 529), ok)
    p = planner.make_plan("hi", [], store, client, log, NOW)
    assert p["errors"] == [] and sleeps == [3.0, 4]


def test_retries_exhausted_raises_friendly_error(store, log, monkeypatch):
    monkeypatch.setattr(planner.time, "sleep", lambda s: None)
    client = FakeClient(*[api_error(anthropic.RateLimitError, 429)] * planner.MAX_ATTEMPTS)
    with pytest.raises(planner.LLMError, match="busy"):
        planner.make_plan("hi", [], store, client, log, NOW)


def test_bad_key_not_retried(store, log):
    client = FakeClient(api_error(anthropic.AuthenticationError, 401))
    with pytest.raises(planner.LLMError, match="key"):
        planner.make_plan("hi", [], store, client, log, NOW)
    assert len(client.calls) == 1
