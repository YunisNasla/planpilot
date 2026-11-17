# PlanPilot: an AI agent for your tasks and calendar

**CS4680 Prompt Engineering, AI Agent Project**

Type what you need in plain English ("move my dentist appointment to Thursday at 10:30", "clear out my completed tasks") and PlanPilot actually does it. Claude reads your request and your current tasks and events, writes a structured plan, PlanPilot checks that plan, asks you before deleting anything, then runs it against a real SQLite database and logs every step.

- **Live demo:** https://planpilot-cs4680.streamlit.app/ (click **🔑 Set API key** in the sidebar and paste your own Anthropic key)

## The problem

Students juggle deadlines and events across classes, work, and life. Most planners make you click through forms for every change. Chatbots can tell you how to reorganize your week, but they can't touch your calendar. PlanPilot closes that gap: the LLM does the understanding, and a small, strict executor does the changing.

## How it works

```
 You type a request
        │
        ▼
 planner.py ──► Claude API  (prompt = rules + action list + today's date + your current tasks/events)
        │          │
        │          ▼
        │     JSON plan: {"reply", "needs_clarification", "actions": [...]}
        ▼
 actions.py  validate: whitelisted actions only, required args, date formats, ids must exist,
             end after start, overlap warnings
        │       └─ invalid? errors go back to Claude once to self-correct, otherwise nothing runs
        ▼
 app.py      safe actions run right away; delete actions wait for "Confirm and run"
        │
        ▼
 store.py    the actor: real SQLite inserts / updates / deletes
        │
        ▼
 logs/audit.jsonl + Activity log tab   (every request, Claude call, retry, plan, action)
```

This follows the same structure as the file organizer from lecture: build the actor first, define a strict text format the LLM writes, then build an executor that turns that text into function calls. The difference is the format: instead of a custom line-based DSL, PlanPilot uses a JSON plan, which is easier to validate field by field.

| File | Role | Assignment requirement |
|---|---|---|
| `store.py` | The actor: SQLite task list and calendar. No AI. | (what the agent acts on) |
| `planner.py` | Builds the prompt, calls Claude, retries with backoff on rate limits and outages, parses JSON, self-corrects once | 1. LLM Integration Module |
| `actions.py` | Action whitelist, plan validator, executor, audit log | 2. Action Interpreter/Executor, 4. Error Handling & Safety |
| `app.py` | Streamlit web UI: chat, plan preview, confirm/cancel, live task/calendar view, activity log | 3. User Interface |
| `tests/` | 30 tests, including the full UI loop with Claude faked | |

### Actions the agent can take

| Action | Destructive? |
|---|---|
| `add_task`, `update_task`, `complete_task`, `reopen_task` | No |
| `delete_task` | **Yes, needs confirmation** |
| `add_event`, `update_event` | No |
| `delete_event` | **Yes, needs confirmation** |

## Safety and error handling

- **Whitelist.** Claude can only name actions from the list above. Anything else (`drop_table`, `send_email`) is rejected before it runs, and the executor double-checks again at run time.
- **Strict validation.** Required arguments, no unknown arguments, `YYYY-MM-DD` / `YYYY-MM-DDTHH:MM` formats, priorities from a fixed set, text length limits, and every `task_id` / `event_id` must exist in the database. Field names never reach SQL unless whitelisted, and all values go through parameterized queries.
- **Self-correction.** If Claude's plan is malformed or fails validation, the exact errors are sent back once. If the second try still fails, nothing is changed and the user sees why.
- **Confirmation for destructive operations.** Any plan containing a delete pauses with a preview ("Delete task #6 'Submit calculus homework 5'") and Confirm/Cancel buttons.
- **Conflict warnings.** Adding or moving an event that overlaps another one shows a warning.
- **API errors and rate limits.** 429s, overloads, 5xx errors, timeouts, and network drops retry with exponential backoff (honoring the `retry-after` header, capped at 20s, up to 4 attempts). A bad API key fails fast with a clear message.
- **One bad step doesn't crash the rest.** Each action runs and reports on its own.
- **Audit log.** Every request, Claude call (latency and tokens), retry, plan, confirmation, cancellation, and action result is appended to `logs/audit.jsonl` and shown live in the Activity log tab, with a download button.
- **Sandboxed demo.** Each browser session gets its own in-memory database seeded with sample data, so visitors can't see or wreck each other's data. Each session is capped at 40 requests to limit API spend.

## Prompt engineering techniques

The full prompt is `SYSTEM_TEMPLATE` in `planner.py`.

1. **Role and boundaries.** Claude is told it's the planning brain, that it never changes data itself, and that separate code validates its output. This keeps it from claiming it already did things.
2. **Grounding with live state.** Every request includes the current tasks and events as JSON, so Claude references real ids instead of guessing.
3. **Date grounding.** The prompt includes the current date, time, time zone, and a list of the next 15 days with weekday names. LLMs are unreliable at "what date is next Friday" on their own; with the lookup list they just read it off.
4. **Generated tool spec.** The action list in the prompt is generated from the same `ACTIONS` dictionary the validator uses, so the prompt and the code can't drift apart.
5. **Strict output format.** One JSON object with a fixed schema, no markdown fences. The parser still tolerates fences or stray text as a fallback.
6. **Few-shot examples.** Four examples cover the key behaviors: multiple actions from one sentence, asking a clarifying question when a request is ambiguous, moving an event by id, and answering a question without taking any action.
7. **Explicit rules for edge cases.** Ambiguity means ask instead of guessing, only delete what was clearly asked, mention overlaps, refuse off-topic requests.
8. **Conversation memory.** The last 3 exchanges (including which actions ran or were cancelled) are sent along, so follow-ups like "actually make it 4pm" work.
9. **Self-correction loop.** Validator errors are fed back to Claude in plain language for one retry.

> Tip for your presentation: keep a short log of prompt changes you try and what they fixed (for example, a request where Claude picked the wrong date before you added the date list). That's exactly the kind of evidence the prompt engineering part of the grade rewards.

## Run it locally

You need Python 3.10+ and an Anthropic API key from https://console.anthropic.com.

```bash
git clone <your repo url>
cd planpilot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml and paste your key
streamlit run app.py
```

It opens at http://localhost:8501. You can also set the key as an environment variable instead (`export ANTHROPIC_API_KEY=...`).

Optional settings (environment variables): `PLANPILOT_MODEL` (default `claude-sonnet-5-5`; `claude-haiku-5-5` is cheaper), `PLANPILOT_TZ` (default `America/Los_Angeles`), `PLANPILOT_MAX_REQUESTS` (default 40).

## Run the tests

```bash
pip install pytest
python -m pytest -q
```

No API key needed. Claude is replaced with a fake that returns scripted plans and scripted errors (rate limits, overloads, bad key, invalid JSON) so every path gets exercised.

## Deploy (free, about 5 minutes)

1. Push this folder to a public GitHub repo. Double check `.streamlit/secrets.toml` is **not** in it (it's in `.gitignore`).
2. Go to https://share.streamlit.io and sign in with GitHub.
3. Click **Create app**, pick your repo and branch, and set the main file to `app.py`.
4. **Don't add any Secrets.** The deployed app has no API key of its own.
5. Click **Deploy**. Your URL will look like `https://your-app-name.streamlit.app`.

Visitors click **🔑 Set API key** in the sidebar and paste their own Anthropic key. The key stays only in that browser session's memory. It's never saved, logged, or shared with other visitors, and it's gone after a refresh.

## Demo video script (about 90 seconds)

1. **(10s) Problem.** "Planners make you click through forms. Chatbots can't touch your calendar. PlanPilot does both."
2. **(20s) Add things.** Type "Schedule a study session tomorrow from 3 to 5pm in the library and remind me to email my advisor by Wednesday." Show both changes appear in the Calendar and Tasks tabs.
3. **(15s) Reschedule.** "Move my dentist appointment to Thursday at 10:30." Point out it found the right event and kept the 1-hour length.
4. **(15s) Ambiguity.** Add two meetings, then say "cancel the meeting." Show it asks which one instead of guessing.
5. **(20s) Safety.** "Clear out my completed tasks." Show the confirmation box listing exactly what will be deleted, then confirm.
6. **(10s) Audit log.** Open the Activity log tab: request, Claude call with latency, plan validation, confirmation, and each action.

## Submission checklist

- [ ] GitHub repo URL
- [x] Deployed Streamlit URL: https://planpilot-cs4680.streamlit.app/ (test it in an incognito window with your own key)
- [ ] 1 to 2 minute demo video (YouTube, unlisted is fine)
- [ ] Submitted through the Google Form
