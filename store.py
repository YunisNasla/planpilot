"""The actor: a small SQLite task list and calendar.

There is no AI in this file. These are plain functions that do real work on a
database, and the agent can only reach them through actions.py.
"""
import sqlite3
from datetime import date, datetime, timedelta

DT_FMT = "%Y-%m-%dT%H:%M"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    title    TEXT NOT NULL,
    due_date TEXT,
    priority TEXT NOT NULL DEFAULT 'medium',
    status   TEXT NOT NULL DEFAULT 'open',
    notes    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    title    TEXT NOT NULL,
    start    TEXT NOT NULL,
    end      TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    notes    TEXT NOT NULL DEFAULT ''
);
"""

# Column names that update_* may touch. Keeps field names out of SQL unless whitelisted.
TASK_FIELDS = {"title", "due_date", "priority", "status", "notes"}
EVENT_FIELDS = {"title", "start", "end", "location", "notes"}


class Store:
    def __init__(self, path=":memory:"):
        # One connection per Streamlit session; Streamlit may rerun on another thread.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---------- helpers ----------
    def _get(self, table, row_id):
        row = self.db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise KeyError(f"{table[:-1]} #{row_id} not found")
        return dict(row)

    def _update(self, table, allowed, row_id, fields):
        self._get(table, row_id)
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {sorted(bad)}")
        if fields:
            sets = ", ".join(f"{name} = ?" for name in fields)
            self.db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), row_id))
            self.db.commit()
        return self._get(table, row_id)

    def _delete(self, table, row_id):
        row = self._get(table, row_id)
        self.db.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        self.db.commit()
        return row

    # ---------- tasks ----------
    def add_task(self, title, due_date=None, priority="medium", notes=""):
        cur = self.db.execute(
            "INSERT INTO tasks (title, due_date, priority, notes) VALUES (?, ?, ?, ?)",
            (title, due_date, priority, notes),
        )
        self.db.commit()
        return self._get("tasks", cur.lastrowid)

    def update_task(self, task_id, **fields):
        return self._update("tasks", TASK_FIELDS, task_id, fields)

    def complete_task(self, task_id):
        return self.update_task(task_id, status="done")

    def reopen_task(self, task_id):
        return self.update_task(task_id, status="open")

    def delete_task(self, task_id):
        return self._delete("tasks", task_id)

    def get_task(self, task_id):
        return self._get("tasks", task_id)

    def list_tasks(self):
        rows = self.db.execute(
            "SELECT * FROM tasks ORDER BY status = 'done', due_date IS NULL, due_date, id"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- events ----------
    def add_event(self, title, start, end=None, location="", notes=""):
        if end is None:
            end = (datetime.strptime(start, DT_FMT) + timedelta(hours=1)).strftime(DT_FMT)
        cur = self.db.execute(
            "INSERT INTO events (title, start, end, location, notes) VALUES (?, ?, ?, ?, ?)",
            (title, start, end, location, notes),
        )
        self.db.commit()
        return self._get("events", cur.lastrowid)

    def update_event(self, event_id, **fields):
        # Moving an event without a new end time keeps its original length.
        if "start" in fields and "end" not in fields:
            old = self._get("events", event_id)
            length = datetime.strptime(old["end"], DT_FMT) - datetime.strptime(old["start"], DT_FMT)
            fields["end"] = (datetime.strptime(fields["start"], DT_FMT) + length).strftime(DT_FMT)
        return self._update("events", EVENT_FIELDS, event_id, fields)

    def delete_event(self, event_id):
        return self._delete("events", event_id)

    def get_event(self, event_id):
        return self._get("events", event_id)

    def list_events(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY start, id").fetchall()]

    def overlapping(self, start, end, exclude_id=None):
        """Events that overlap [start, end). ISO strings in one format sort correctly."""
        rows = self.db.execute(
            "SELECT * FROM events WHERE start < ? AND end > ? AND id IS NOT ?",
            (end, start, exclude_id),
        ).fetchall()
        return [dict(r) for r in rows]


def seed_demo(store: Store, today: date):
    """Sample data so the deployed demo has something to work with."""
    def d(days):
        return (today + timedelta(days=days)).isoformat()

    def at(days, hhmm):
        return f"{d(days)}T{hhmm}"

    store.add_task("Finish CS4680 agent demo video", d(3), "high")
    store.add_task("Write physics lab report", d(1), "high", "Lab 6: projectile motion")
    store.add_task("Read chapter 7 for ME 2140", d(4), "medium")
    store.add_task("Renew parking permit", d(9), "low")
    store.add_task("Email advisor about spring classes", None, "medium")
    done = store.add_task("Submit calculus homework 5", d(-2), "medium")
    store.complete_task(done["id"])
    done = store.add_task("Buy printer filament", None, "low")
    store.complete_task(done["id"])

    store.add_event("CS4680 lecture", at(1, "17:30"), at(1, "18:45"), "Building 8")
    store.add_event("Dentist appointment", at(2, "09:00"), at(2, "10:00"), "Cerritos Dental")
    store.add_event("Study group: physics", at(2, "15:00"), at(2, "16:30"), "Library 3rd floor")
    store.add_event("Gym", at(3, "07:00"), at(3, "08:00"))
    store.add_event("Club meeting", at(5, "12:00"), at(5, "13:00"), "Bronco Student Center")
