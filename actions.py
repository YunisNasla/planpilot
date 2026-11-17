"""Action interpreter/executor.

Claude never touches the database. It writes a JSON plan, this file checks every
action against a whitelist and the current data, and only then calls Store.
"""
import json
import os
from datetime import datetime, timedelta, timezone

from store import DT_FMT

DATE_FMT = "%Y-%m-%d"
PRIORITIES = ("low", "medium", "high")
MAX_ACTIONS = 25
MAX_TEXT = 300

# The whitelist. Each action maps 1:1 to a Store method of the same name.
# "destructive" actions always wait for the user to confirm.
ACTIONS = {
    "add_task": {
        "required": ["title"],
        "optional": ["due_date", "priority", "notes"],
        "destructive": False,
        "doc": "Create a task. priority defaults to medium.",
    },
    "update_task": {
        "required": ["task_id"],
        "optional": ["title", "due_date", "priority", "notes"],
        "destructive": False,
        "doc": "Change fields of an existing task. due_date may be null to clear it.",
    },
    "complete_task": {
        "required": ["task_id"],
        "optional": [],
        "destructive": False,
        "doc": "Mark a task as done.",
    },
    "reopen_task": {
        "required": ["task_id"],
        "optional": [],
        "destructive": False,
        "doc": "Mark a done task as open again.",
    },
    "delete_task": {
        "required": ["task_id"],
        "optional": [],
        "destructive": True,
        "doc": "Permanently delete a task.",
    },
    "add_event": {
        "required": ["title", "start"],
        "optional": ["end", "location", "notes"],
        "destructive": False,
        "doc": "Create a calendar event. end defaults to start + 1 hour.",
    },
    "update_event": {
        "required": ["event_id"],
        "optional": ["title", "start", "end", "location", "notes"],
        "destructive": False,
        "doc": "Change an event. Moving start without end keeps the same length.",
    },
    "delete_event": {
        "required": ["event_id"],
        "optional": [],
        "destructive": True,
        "doc": "Permanently delete a calendar event.",
    },
}

ARG_FORMATS = {
    "task_id": "integer id from CURRENT STATE",
    "event_id": "integer id from CURRENT STATE",
    "title": "string",
    "notes": "string",
    "location": "string",
    "due_date": "YYYY-MM-DD",
    "priority": "low | medium | high",
    "start": "YYYY-MM-DDTHH:MM (24-hour, local time)",
    "end": "YYYY-MM-DDTHH:MM (24-hour, local time)",
}


def catalog_text():
    """The action list as the prompt shows it to Claude."""
    lines = []
    for name, spec in ACTIONS.items():
        args = [f"{a}*" for a in spec["required"]] + spec["optional"]
        flag = "  [DESTRUCTIVE: user must confirm]" if spec["destructive"] else ""
        lines.append(f"- {name}({', '.join(args)}): {spec['doc']}{flag}")
    lines.append("  (* = required)")
    lines.append("Argument formats:")
    lines += [f"- {k}: {v}" for k, v in ARG_FORMATS.items()]
    return "\n".join(lines)


# ---------------- validation ----------------

def _check_arg(name, value):
    """Return an error string, or None if the value is fine."""
    if name in ("task_id", "event_id"):
        return None if isinstance(value, int) and not isinstance(value, bool) else "must be an integer id"
    if name == "due_date":
        if value is None:
            return None
        return _check_time(value, DATE_FMT)
    if name in ("start", "end"):
        return _check_time(value, DT_FMT)
    if name == "priority":
        return None if value in PRIORITIES else f"must be one of {', '.join(PRIORITIES)}"
    # title, notes, location
    if not isinstance(value, str):
        return "must be a string"
    if name == "title" and not value.strip():
        return "cannot be empty"
    if len(value) > MAX_TEXT:
        return f"is longer than {MAX_TEXT} characters"
    return None


def _check_time(value, fmt):
    try:
        datetime.strptime(value, fmt)
        return None
    except (TypeError, ValueError):
        return f"must look like {'YYYY-MM-DD' if fmt == DATE_FMT else 'YYYY-MM-DDTHH:MM'}"


def validate_plan(data, store):
    """Check Claude's JSON plan before anything runs.

    Returns {"reply", "needs_clarification", "actions", "errors", "warnings"}.
    The plan may only run if errors is empty.
    """
    plan = {"reply": "", "needs_clarification": False, "actions": [], "errors": [], "warnings": []}
    errors, warnings = plan["errors"], plan["warnings"]

    if not isinstance(data, dict):
        errors.append("Top level must be a JSON object.")
        return plan
    plan["reply"] = str(data.get("reply", "")).strip()
    plan["needs_clarification"] = bool(data.get("needs_clarification", False))
    actions = data.get("actions", [])

    if not isinstance(actions, list):
        errors.append('"actions" must be a list.')
        return plan
    if len(actions) > MAX_ACTIONS:
        errors.append(f"Too many actions ({len(actions)}). The limit is {MAX_ACTIONS} per request.")
        return plan
    if plan["needs_clarification"] and actions:
        errors.append("A plan that asks a clarifying question must have an empty actions list.")
    if not plan["reply"]:
        errors.append('"reply" must be a non-empty message for the user.')

    task_ids = {t["id"] for t in store.list_tasks()}
    event_ids = {e["id"] for e in store.list_events()}

    for i, act in enumerate(actions, 1):
        where = f"Action {i}"
        if not isinstance(act, dict):
            errors.append(f"{where}: must be an object.")
            continue
        name, args = act.get("action"), act.get("args", {})
        spec = ACTIONS.get(name)
        if spec is None:
            errors.append(f"{where}: unknown action {name!r}. Allowed: {', '.join(ACTIONS)}.")
            continue
        if not isinstance(args, dict):
            errors.append(f"{where} ({name}): args must be an object.")
            continue
        where = f"{where} ({name})"

        missing = [a for a in spec["required"] if a not in args]
        unknown = [a for a in args if a not in spec["required"] + spec["optional"]]
        if missing:
            errors.append(f"{where}: missing {', '.join(missing)}.")
        if unknown:
            errors.append(f"{where}: unknown argument(s) {', '.join(unknown)}.")
        if missing or unknown:
            continue

        bad = [f"{a} {msg}" for a, v in args.items() if (msg := _check_arg(a, v))]
        if bad:
            errors.append(f"{where}: " + "; ".join(bad) + ".")
            continue

        if "task_id" in args and args["task_id"] not in task_ids:
            errors.append(f"{where}: task #{args['task_id']} does not exist.")
            continue
        if "event_id" in args and args["event_id"] not in event_ids:
            errors.append(f"{where}: event #{args['event_id']} does not exist.")
            continue
        if name.startswith("update_") and len(args) == 1:
            errors.append(f"{where}: nothing to change.")
            continue
        if name in ("add_event", "update_event"):
            err = _check_event_times(name, args, store, warnings)
            if err:
                errors.append(f"{where}: {err}")
                continue

        plan["actions"].append({"action": name, "args": args, "destructive": spec["destructive"]})
    return plan


def _check_event_times(name, args, store, warnings):
    old = store.get_event(args["event_id"]) if name == "update_event" else {}
    start = args.get("start", old.get("start"))
    end = args.get("end")
    if end is None:  # mirror Store's defaults so the conflict check sees the real slot
        if name == "add_event":
            end = (datetime.strptime(start, DT_FMT) + timedelta(hours=1)).strftime(DT_FMT)
        elif "start" in args:
            length = datetime.strptime(old["end"], DT_FMT) - datetime.strptime(old["start"], DT_FMT)
            end = (datetime.strptime(start, DT_FMT) + length).strftime(DT_FMT)
        else:
            end = old["end"]
    if end <= start:
        return f"end ({end}) must be after start ({start})."
    if "start" in args or "end" in args:
        for other in store.overlapping(start, end, exclude_id=args.get("event_id")):
            title = args.get("title", old.get("title"))
            warnings.append(
                f"“{title}” ({start[11:]}–{end[11:]} on {start[:10]}) overlaps "
                f"“{other['title']}” ({other['start'][11:]}–{other['end'][11:]})."
            )
    return None


# ---------------- execution ----------------

def execute(plan_actions, store, log):
    """Run validated actions one by one. One failure doesn't stop the rest."""
    results = []
    for act in plan_actions:
        name, args = act["action"], act["args"]
        try:
            if name not in ACTIONS:  # defense in depth: only whitelisted methods are callable
                raise ValueError(f"action {name!r} is not allowed")
            row = getattr(store, name)(**args)
            results.append({**act, "ok": True, "result": row})
            log.write("action", action=name, args=args, ok=True, result=row)
        except Exception as e:  # report and keep going; nothing here should crash the app
            results.append({**act, "ok": False, "error": str(e)})
            log.write("action", action=name, args=args, ok=False, error=str(e))
    return results


def describe(act, store=None):
    """One plain-English line for an action, used in the plan preview and results."""
    a = act["args"]
    name = act["action"]

    def label(kind, row_id):
        if store is None:
            return f"{kind} #{row_id}"
        try:
            row = store.get_task(row_id) if kind == "task" else store.get_event(row_id)
            return f"{kind} #{row_id} “{row['title']}”"
        except KeyError:
            return f"{kind} #{row_id}"

    def changes():
        return ", ".join(f"{k} → {v if v is not None else 'none'}" for k, v in a.items() if not k.endswith("_id"))

    if name == "add_task":
        extra = ", ".join(x for x in [a.get("due_date") and f"due {a['due_date']}", a.get("priority")] if x)
        return f"Add task “{a['title']}”" + (f" ({extra})" if extra else "")
    if name == "update_task":
        return f"Update {label('task', a['task_id'])}: {changes()}"
    if name == "complete_task":
        return f"Mark {label('task', a['task_id'])} as done"
    if name == "reopen_task":
        return f"Reopen {label('task', a['task_id'])}"
    if name == "delete_task":
        return f"Delete {label('task', a['task_id'])}"
    if name == "add_event":
        when = a["start"].replace("T", " ") + (f"–{a['end'][11:]}" if a.get("end") else "")
        where = f" at {a['location']}" if a.get("location") else ""
        return f"Add event “{a['title']}” on {when}{where}"
    if name == "update_event":
        return f"Update {label('event', a['event_id'])}: {changes()}"
    if name == "delete_event":
        return f"Delete {label('event', a['event_id'])}"
    return f"{name} {a}"


# ---------------- audit log ----------------

class AuditLog:
    """Appends every LLM call and action to a JSON Lines file and keeps a copy for the UI."""

    def __init__(self, session_id, path="logs/audit.jsonl"):
        self.session_id = session_id
        self.path = path
        self.entries = []
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, kind, **data):
        entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "session": self.session_id, "kind": kind, **data}
        self.entries.append(entry)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass  # a read-only disk shouldn't break the app; the in-memory copy still shows in the UI
        return entry
