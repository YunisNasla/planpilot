"""LLM integration: build the prompt, call Claude, parse and validate its plan."""
import json
import os
import re
import time
from datetime import timedelta

import anthropic

from actions import catalog_text, validate_plan

MODEL = os.environ.get("PLANPILOT_MODEL", "claude-sonnet-5-5")
MAX_ATTEMPTS = 4       # API retries for rate limits / overloads / network errors
MAX_BACKOFF_S = 20
HISTORY_TURNS = 3      # past user+assistant pairs sent for follow-ups like "move it to 4pm"

# Rate limits (429), overloads and 5xx (InternalServerError covers 500/503/504/529),
# timeouts and network drops are worth retrying. Anything else won't fix itself.
RETRYABLE = (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError)


class LLMError(Exception):
    """A failure with a message that is safe to show the user."""


SYSTEM_TEMPLATE = """You are PlanPilot, the planning brain of a task and calendar agent for a college student.
You never change data yourself. You read the user's request and the CURRENT STATE, then write a JSON plan.
Separate code validates your plan and runs it against a real database, so precision matters.

# Right now
{now}
Upcoming dates (use these to resolve words like "Friday" or "next Tuesday"):
{calendar}

# Actions you can use
{catalog}

# CURRENT STATE
Tasks:
{tasks}
Events:
{events}

# Rules
1. Use only the actions listed above, with exactly the argument names shown.
2. Only use task_id / event_id values that appear in CURRENT STATE. Never guess an id.
3. Match items by meaning, not exact wording ("the dentist" matches "Dentist appointment").
4. If the request could refer to more than one item, or a required detail is missing and has no
   sensible default (like a time for an event), ask a short question: set needs_clarification to true
   and return an empty actions list. Don't ask when a reasonable default exists.
5. Resolve relative dates ("tomorrow", "this Friday", "in 2 weeks") using "Right now" and the date list.
   "Next <weekday>" means that weekday in the following week. Times are local and 24-hour in the JSON.
6. Only delete things the user clearly asked to delete. "Clear my completed tasks" means delete each
   task whose status is done, one delete_task action per task.
7. If the request is a question ("what's due this week?"), answer it in reply from CURRENT STATE and
   return an empty actions list.
8. If a new or moved event overlaps an existing one, still do what the user asked but mention the
   overlap in reply.
9. reply is one or two friendly sentences, written as if the plan will succeed ("Added...", "Moved...").
   For deletes, say what will be removed. Don't list ids.
10. Refuse anything outside tasks and calendar (writing essays, coding, etc.) with a short reply and no actions.

# Output format
Return ONLY a JSON object, no markdown fences and no text before or after it:
{{"reply": "<message to the user>", "needs_clarification": <true|false>, "actions": [{{"action": "<name>", "args": {{...}}}}]}}

# Examples (ids here are made up; always use ids from CURRENT STATE)
Today is Mon 2026-03-02.
User: remind me to call mom tomorrow, and put gym on my calendar friday at 6pm
{{"reply": "Added a reminder to call mom tomorrow and put gym on Friday at 6 PM.", "needs_clarification": false, "actions": [{{"action": "add_task", "args": {{"title": "Call mom", "due_date": "2026-03-03"}}}}, {{"action": "add_event", "args": {{"title": "Gym", "start": "2026-03-06T18:00"}}}}]}}

State has events #4 "Team meeting" and #9 "Advisor meeting".
User: cancel the meeting
{{"reply": "Which one should I cancel, the team meeting or the advisor meeting?", "needs_clarification": true, "actions": []}}

State has event #2 "Dentist" on 2026-03-03T09:00-10:00.
User: push the dentist to thursday at 10:30
{{"reply": "Moved your dentist appointment to Thursday at 10:30 AM.", "needs_clarification": false, "actions": [{{"action": "update_event", "args": {{"event_id": 2, "start": "2026-03-05T10:30"}}}}]}}

State has open tasks #3 "Lab report" (due 2026-03-04) and #6 "Read ch. 2" (due 2026-03-20).
User: what's due this week?
{{"reply": "Just the lab report, due Wednesday.", "needs_clarification": false, "actions": []}}
"""


def build_system_prompt(store, now):
    tz = now.tzname() or ""
    days = [now.date() + timedelta(days=i) for i in range(15)]
    calendar = ", ".join(f"{d:%a} {d.isoformat()}" for d in days)
    tasks = [{k: t[k] for k in ("id", "title", "due_date", "priority", "status", "notes")} for t in store.list_tasks()]
    events = [{k: e[k] for k in ("id", "title", "start", "end", "location")} for e in store.list_events()]
    return SYSTEM_TEMPLATE.format(
        now=f"{now:%A, %B %d, %Y, %I:%M %p} ({tz}). Today's date is {now.date().isoformat()}.",
        calendar=calendar,
        catalog=catalog_text(),
        tasks="\n".join(json.dumps(t) for t in tasks) or "(none)",
        events="\n".join(json.dumps(e) for e in events) or "(none)",
    )


def make_client(api_key):
    # SDK retries are off so the retry/backoff below is the only one (and shows up in the log).
    return anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=60)


def call_claude(client, system, messages, log):
    """Send one request with exponential backoff on retryable errors. Returns the reply text."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.time()
        try:
            resp = client.messages.create(model=MODEL, max_tokens=2000, system=system, messages=messages)
            log.write("llm_call", model=MODEL, attempt=attempt, ms=int((time.time() - started) * 1000),
                      input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)
            return "".join(block.text for block in resp.content if block.type == "text")
        except anthropic.AuthenticationError:
            log.write("llm_error", error="authentication")
            raise LLMError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY.")
        except RETRYABLE as e:
            wait = min(_retry_after(e) or 2 ** attempt, MAX_BACKOFF_S)
            log.write("llm_retry", attempt=attempt, error=type(e).__name__, wait_s=wait)
            if attempt == MAX_ATTEMPTS:
                raise LLMError("Claude is busy or unreachable right now. Please try again in a minute.")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            log.write("llm_error", status=e.status_code, error=str(e))
            raise LLMError(f"The API rejected the request ({e.status_code}).")


def _retry_after(err):
    """Honor the server's retry-after header when it sends one."""
    try:
        return float(err.response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None


def parse_json(text):
    """Pull the JSON object out of Claude's reply, tolerating code fences or stray text."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("No JSON object found in the response.")
    return json.loads(text[start:end + 1])


def make_plan(request, history, store, client, log, now):
    """Turn a natural-language request into a validated plan.

    If the first plan is malformed or fails validation, the errors go back to Claude
    once so it can fix them (self-correction). Raises LLMError on API failure.
    """
    system = build_system_prompt(store, now)
    messages = history[-2 * HISTORY_TURNS:] + [{"role": "user", "content": request}]
    log.write("request", text=request)

    for attempt in (1, 2):
        raw = call_claude(client, system, messages, log)
        try:
            plan = validate_plan(parse_json(raw), store)
        except ValueError as e:  # json.JSONDecodeError is a ValueError
            plan = {"reply": "", "needs_clarification": False, "actions": [], "warnings": [],
                    "errors": [f"Response was not valid JSON: {e}"]}
        log.write("plan", attempt=attempt, raw=raw, errors=plan["errors"], warnings=plan["warnings"])
        if not plan["errors"] or attempt == 2:
            plan["self_corrected"] = attempt == 2 and not plan["errors"]
            return plan
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "Your plan was rejected by the validator:\n- "
                + "\n- ".join(plan["errors"])
                + "\nReturn the corrected JSON object only."},
        ]
