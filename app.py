"""PlanPilot web UI (Streamlit). Run locally with: streamlit run app.py"""
import json
import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from actions import AuditLog, describe, execute
from planner import MODEL, LLMError, make_client, make_plan
from store import Store, seed_demo

TZ = ZoneInfo(os.environ.get("PLANPILOT_TZ", "America/Los_Angeles"))
MAX_REQUESTS = int(os.environ.get("PLANPILOT_MAX_REQUESTS", "40"))  # caps API spend per visitor

EXAMPLES = [
    "Add a high priority task to finish the project slides by Friday",
    "Schedule a study session tomorrow from 3 to 5pm in the library",
    "What do I have due this week?",
    "Move my dentist appointment to Thursday at 10:30",
    "I finished the lab report",
    "Clear out my completed tasks",
]

st.set_page_config(page_title="PlanPilot", page_icon="🗓️", layout="wide")
ss = st.session_state


def now():
    return datetime.now(TZ)


def api_key():
    if ss.get("user_key"):  # pasted by the visitor, lives only in this session's memory
        return ss.user_key
    try:
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:  # no secrets.toml locally is normal
        return os.environ.get("ANTHROPIC_API_KEY")


def init(reset=False):
    """Each browser session gets its own sandbox database seeded with demo data."""
    if reset or "store" not in ss:
        ss.store = Store()
        seed_demo(ss.store, now().date())
        ss.chat = []      # what the UI shows
        ss.history = []   # what Claude sees for follow-up requests
        ss.pending = None
        ss.log = AuditLog(uuid.uuid4().hex[:8])
        ss.setdefault("requests", 0)  # survives reset so the cap can't be dodged


def say(text, items=None, warnings=None, kind="info"):
    ss.chat.append({"role": "assistant", "text": text, "items": items or [], "warnings": warnings or [], "kind": kind})


def remember(request, outcome):
    ss.history += [{"role": "user", "content": request}, {"role": "assistant", "content": outcome}]


# ---------------- agent loop ----------------

def handle(request):
    ss.chat.append({"role": "user", "text": request})
    if ss.requests >= MAX_REQUESTS:
        say(f"This demo session hit its limit of {MAX_REQUESTS} requests. Refresh the page to start a new one.", kind="error")
        return
    key = api_key()
    if not key:
        say("No API key set. Click **🔑 Set API key** in the sidebar.", kind="error")
        return

    ss.requests += 1
    try:
        with st.spinner("Planning…"):
            plan = make_plan(request, ss.history, ss.store, make_client(key), ss.log, now())
    except LLMError as e:
        say(str(e), kind="error")
        return

    if plan["errors"]:
        say("I couldn't build a safe plan for that, so nothing was changed.",
            items=[("⚠️", e) for e in plan["errors"]], kind="error")
        remember(request, "(My plan failed validation, nothing was changed.)")
        return
    if not plan["actions"]:
        say(plan["reply"], kind="question" if plan["needs_clarification"] else "info")
        remember(request, plan["reply"])
        return

    plan["lines"] = [describe(a, ss.store) for a in plan["actions"]]  # before running, so titles of deleted items still resolve
    if any(a["destructive"] for a in plan["actions"]):
        ss.pending = {"request": request, "plan": plan}
        ss.log.write("awaiting_confirmation", actions=plan["actions"])
        return
    run(request, plan)


def run(request, plan):
    results = execute(plan["actions"], ss.store, ss.log)
    items = [("✅", line) if r["ok"] else ("❌", f"{line}: {r['error']}") for line, r in zip(plan["lines"], results)]
    ok = all(r["ok"] for r in results)
    text = plan["reply"] if ok else "Some steps failed. Here's what happened:"
    say(text, items=items, warnings=plan["warnings"], kind="done" if ok else "error")
    remember(request, f"{plan['reply']}\n[Executed: {json.dumps([{k: r[k] for k in ('action', 'args', 'ok')} for r in results])}]")


def confirm():
    p = ss.pop("pending")
    ss.pending = None
    ss.log.write("confirmed")
    run(p["request"], p["plan"])


def cancel():
    p = ss.pop("pending")
    ss.pending = None
    ss.log.write("cancelled", actions=p["plan"]["actions"])
    say("Cancelled. Nothing was changed.")
    remember(p["request"], "(The user cancelled this plan. Nothing was changed.)")


# ---------------- rendering ----------------

def render_message(m):
    avatar = "🧑‍🎓" if m["role"] == "user" else "🗓️"
    with st.chat_message(m["role"], avatar=avatar):
        if m["role"] == "user":
            st.markdown(m["text"])
            return
        if m["kind"] == "error":
            st.error(m["text"])
        elif m["kind"] == "question":
            st.info(m["text"], icon="❓")
        else:
            st.markdown(m["text"])
        for icon, line in m["items"]:
            st.markdown(f"{icon} {line}")
        for w in m["warnings"]:
            st.warning(w, icon="⏰")


def render_pending():
    plan = ss.pending["plan"]
    with st.chat_message("assistant", avatar="🗓️"), st.container(border=True):
        st.markdown(f"**{plan['reply']}**")
        st.caption("This includes changes that can't be undone. Review and confirm:")
        for act, line in zip(plan["actions"], plan["lines"]):
            st.markdown(f"{'🗑️' if act['destructive'] else '➕'} {line}")
        for w in plan["warnings"]:
            st.warning(w, icon="⏰")
        c1, c2 = st.columns(2)
        c1.button("Confirm and run", type="primary", on_click=confirm, width="stretch")
        c2.button("Cancel", on_click=cancel, width="stretch")


def day_label(d, today):
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return f"{d:%a, %b} {d.day}"


def render_tasks(today):
    tasks = ss.store.list_tasks()
    open_tasks = [t for t in tasks if t["status"] == "open"]
    done = [t for t in tasks if t["status"] == "done"]

    def row(t):
        due = ""
        if t["due_date"]:
            d = datetime.strptime(t["due_date"], "%Y-%m-%d").date()
            due = day_label(d, today) + (" (overdue)" if d < today and t["status"] == "open" else "")
        icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}[t["priority"]]
        return {"": icon, "#": t["id"], "Task": t["title"], "Due": due}

    cols = {"": st.column_config.TextColumn(width=30, help="Priority: 🔴 high, 🟡 medium, 🟢 low"),
            "#": st.column_config.NumberColumn(width=40)}
    if open_tasks:
        st.dataframe([row(t) for t in open_tasks], hide_index=True, column_config=cols)
        st.caption("🔴 high · 🟡 medium · 🟢 low priority")
    else:
        st.caption("No open tasks. Nice.")
    with st.expander(f"Completed ({len(done)})"):
        if done:
            st.dataframe([row(t) for t in done], hide_index=True, column_config=cols)


def render_calendar(today):
    events = [e for e in ss.store.list_events() if e["end"] >= now().strftime("%Y-%m-%dT%H:%M")]
    if not events:
        st.caption("Nothing on the calendar.")
        return
    current = None
    for e in events:
        d = datetime.strptime(e["start"][:10], "%Y-%m-%d").date()
        if d != current:
            current = d
            st.markdown(f"**{day_label(d, today)}**")
        where = f" · {e['location']}" if e["location"] else ""
        st.markdown(f"`{e['start'][11:]}–{e['end'][11:]}` {e['title']}{where} <span style='opacity:.5'>#{e['id']}</span>",
                    unsafe_allow_html=True)


def render_log():
    entries = ss.log.entries
    if not entries:
        st.caption("Every request, Claude call, and action will show up here.")
        return

    def summary(e):
        k = e["kind"]
        if k == "request":
            return f"User: {e['text']}"
        if k == "llm_call":
            return f"Claude replied in {e['ms']} ms ({e['input_tokens']} in / {e['output_tokens']} out tokens)"
        if k == "llm_retry":
            return f"{e['error']} on attempt {e['attempt']}, retrying in {e['wait_s']}s"
        if k == "llm_error":
            return f"API error: {e['error']}"
        if k == "plan":
            return ("Plan passed validation" if not e["errors"] else "Plan rejected: " + "; ".join(e["errors"])) + \
                   (" (after self-correction)" if e["attempt"] == 2 else "")
        if k == "awaiting_confirmation":
            return f"Paused: {len(e['actions'])} action(s) need user confirmation"
        if k == "action":
            status = "OK" if e["ok"] else f"FAILED ({e['error']})"
            return f"{e['action']} {json.dumps(e['args'])}: {status}"
        return k.capitalize()

    st.dataframe([{"Time (UTC)": e["time"][11:19], "What happened": summary(e)} for e in reversed(entries)],
                 hide_index=True, height=360,
                 column_config={"Time (UTC)": st.column_config.TextColumn(width="small")})
    st.download_button("Download full log (JSONL)", "\n".join(json.dumps(e, default=str) for e in entries),
                       file_name=f"planpilot-audit-{ss.log.session_id}.jsonl", mime="application/json")


@st.dialog("Anthropic API key")
def key_dialog():
    key = st.text_input("API key", type="password", placeholder="sk-ant-...")
    st.caption("Kept only in this browser session's memory. Never saved or logged. "
               "Get a key at https://console.anthropic.com")
    save, clear = st.columns(2)
    if save.button("Save", type="primary", width="stretch"):
        if key.strip():
            ss.user_key = key.strip()
            st.rerun()
        st.error("Paste a key first.")
    if clear.button("Clear", width="stretch"):
        ss.pop("user_key", None)
        st.rerun()


# ---------------- page ----------------

init()
today = now().date()

with st.sidebar:
    if st.button("Change API key" if ss.get("user_key") else "Set API key", icon="🔑", width="stretch"):
        key_dialog()
    st.caption("Key set ✓" if ss.get("user_key") else "No key set")
    st.divider()
    st.subheader("Try asking")
    for ex in EXAMPLES:
        if st.button(ex, width="stretch", disabled=ss.pending is not None):
            ss.queued = ex
    st.divider()
    if st.button("Reset demo data", icon="🔄", width="stretch"):
        init(reset=True)
        st.rerun()
    st.caption(f"Model: `{MODEL}`  \nRequests this session: {ss.requests}/{MAX_REQUESTS}  \n"
               f"Session: `{ss.log.session_id}`")
    st.caption("Your data lives in a private sandbox for this browser tab and resets when you refresh.")

st.title("🗓️ PlanPilot")
st.caption("Tell it what you need in plain English. Claude plans the changes, PlanPilot checks them, "
           "asks before deleting anything, then updates your tasks and calendar.")

left, right = st.columns([5, 4], gap="large")

with right:
    tasks = ss.store.list_tasks()
    week_end = (today + timedelta(days=7)).isoformat()
    m1, m2, m3 = st.columns(3)
    m1.metric("Open tasks", sum(t["status"] == "open" for t in tasks))
    m2.metric("Due in 7 days", sum(bool(t["status"] == "open" and t["due_date"] and t["due_date"] <= week_end) for t in tasks))
    m3.metric("Events in 7 days", sum(today.isoformat() <= e["start"][:10] <= week_end for e in ss.store.list_events()))
    tab_tasks, tab_cal, tab_log = st.tabs(["Tasks", "Calendar", "Activity log"])
    with tab_tasks:
        render_tasks(today)
    with tab_cal:
        render_calendar(today)
    with tab_log:
        render_log()

with left:
    if not ss.chat:
        with st.chat_message("assistant", avatar="🗓️"):
            st.markdown("Hi! I can add, update, complete, and delete tasks and calendar events. "
                        "Try something like *“put gym on my calendar Friday at 7am”* or pick an example on the left.")
    for m in ss.chat:
        render_message(m)
    if ss.pending:
        render_pending()

    prompt = st.chat_input("Tell PlanPilot what to do…", max_chars=500, disabled=ss.pending is not None)
    prompt = prompt or ss.pop("queued", None)
    if prompt and not ss.pending:
        with st.chat_message("user", avatar="🧑‍🎓"):
            st.markdown(prompt)
        handle(prompt)
        st.rerun()
