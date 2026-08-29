# Follow-Up Agent

The follow-up agent manages LinkedIn DM conversations with connected leads. It
runs as a self-rescheduling loop: every decision that isn't `mark_completed`
creates a new Task, so the daemon keeps checking in on each conversation until
the deal is closed.

## Pipeline Overview

```
CONNECTED lead
    │
    ▼
scheduler.on_deal_state_entered() ← fired from set_profile_state(CONNECTED)
    │
    ▼
enqueue_follow_up()          ← in linkedin/tasks/scheduler.py
    │
    ▼
daemon picks up Task
    │
    ▼
handle_follow_up()           ← linkedin/tasks/follow_up.py
    ├─ rate limit check      ← LinkedInProfile.can_execute(FOLLOW_UP)
    ├─ materialize profile summary (lazy, once per lead×campaign)
    ├─ sync conversation     ← Voyager API → ChatMessage upsert → chat_summary update
    └─ run_follow_up_agent() ← linkedin/agents/follow_up.py
         │
         ▼
    FollowUpDecision
    ┌────────────────────────────────────────────────────────┐
    │ send_message   → send DM, record action, re-enqueue   │
    │ wait           → re-enqueue (no message sent)          │
    │ mark_completed → close Deal with structured outcome    │
    │ handoff        → holding DM, Deal→HANDOFF, alert a      │
    │                  human, enqueue NOTHING                 │
    └────────────────────────────────────────────────────────┘
```

## FollowUpDecision

Structured LLM output defined in `linkedin/agents/follow_up.py`:

| Field | Type | Required When |
|-------|------|---------------|
| `action` | `"send_message"` / `"mark_completed"` / `"wait"` / `"handoff"` | always |
| `message` | `str` | `send_message`, `handoff` |
| `outcome` | `Outcome` enum | `mark_completed` |
| `follow_up_hours` | `float` | always (agent decides the pace; ignored for `handoff`) |

Outcome values: `converted`, `not_interested`, `wrong_fit`, `no_budget`,
`has_solution`, `bad_timing`, `unresponsive`.

Validated by a Pydantic `model_validator` — the LLM call fails if required
fields are missing for the chosen action.

New choices go on the existing `action` Literal, never as a new field. On the
codex path `codex_client._strictify_schema` forces `required` to list every
property, so an added field would make the model rule on it on every single
call — and one bad verdict fails the task with no retry.

## Conversation Stages

The mode is decided in Python, not by the model: it sees only
`RECENT_MESSAGES_WINDOW` (6) messages, and `chat_summary` deliberately holds no
facts about our own turns, so it cannot know how far the conversation has
travelled. `_conversation_stage()` reads the whole deal-scoped history and the
template renders exactly one mode.

| Stage | When | What the prompt says |
|-------|------|----------------------|
| `opening` | the lead has not answered substantively *after we spoke* | one Mom-Test discovery question |
| `answer_first` | the lead's last message is a question | answer honestly, thank them, do not pitch |
| `qualify` | the lead engaged, and we have spent fewer than `PIVOT_WINDOW_TURNS` turns since | ask the commercial question, rewritten in their language |
| `advance` | the commercial question is already out | never re-ask it; work toward a next step |

`answer_first` is checked before the counters, so a lead asking "why are you
asking me this?" always gets an answer instead of a pitch. `opening` requires
that *we* spoke first, otherwise a lead who opens the thread would be met with
a commercial question as our very first line. History older than
`deal.creation_date` is excluded — these accounts carry months of human
conversations that must not look like engagement the bot earned.

The question text comes from `Campaign.qualifying_question` when an operator
filled it, otherwise from `agents/follow_up_defaults.py`. Blank is the normal
state and means "use the repo default", so the copy is not duplicated into
every account's SQLite file where it would silently drift.

## Not Sounding Like a Bot

Two independent guards against the same failure — consecutive messages opening
with the same formula ("Понял, спасибо за уточнение."):

- **Prompt side.** `previous_openers` lists the first line of our last five
  messages with an instruction not to reuse them, and `## Voice` bans
  acknowledgment openers outright.
- **Python side.** `_strip_ack_opener()` removes the formula structurally after
  the decision — no LLM call, cannot raise, always terminates. It handles both
  the comma-joined and dash-joined forms, and returns the message untouched
  when only a fragment would remain.

The Python half exists because a prompt rule catches the *second* occurrence at
best; this one catches the first.

## Agent Context

The agent sees a rich prompt rendered from `follow_up_agent.j2` with:

| Section | Source | Built When |
|---------|--------|------------|
| Seller identity (`self_name`) | `session.self_profile` | every call |
| Product docs, campaign objective, booking link | `Campaign` model | every call |
| Profile facts | `Deal.profile_summary` (JSON fact list) | lazy, once per lead×campaign |
| Chat facts | `Deal.chat_summary` (JSON fact list) | incremental, on each sync |
| Recent messages (verbatim, with age) | last 6 `ChatMessage` rows | every call |
| `days_since_last_outgoing` | computed from messages | every call |
| `unanswered_outgoing` count | trailing run of outgoing messages | every call |

The split between **summary facts** (durable, LLM-extracted) and **verbatim
messages** (recent window) lets the agent reason about the full conversation
history without overflowing the context with old messages.

## Summaries Pipeline

Both summaries live on the `Deal` model as JSON fact lists (`{"facts": [...]}`).
All LLM calls go through `linkedin/db/summaries.py`.

### Profile Summary

`materialize_profile_summary_if_missing(deal, session)`:

1. No-op if `deal.profile_summary` is already populated
2. Re-scrapes the lead's LinkedIn profile via Voyager API
3. Extracts facts via LLM, conditioned on the campaign objective and product docs
4. Persists on `Deal.profile_summary`

Runs **once** per `(lead, campaign)` lifetime — the first time a follow-up
touches the deal.

### Chat Summary

`update_chat_summary(deal, new_messages)`:

1. Called by `sync_conversation()` after upserting new `ChatMessage` rows
2. Formats new messages as a labeled transcript (`[Me]` / `[Lead]`)
3. Short-circuits if there are no incoming (lead) messages — a burst of outgoing
   messages alone doesn't trigger an LLM call
4. Extracts new facts via LLM (`extract_facts`)
5. Reconciles against existing facts via `reconcile_facts()` — mem0-style
   ADD/UPDATE/DELETE/NONE events, not naive append-and-dedup
6. Persists updated list on `Deal.chat_summary`

The reconciliation step uses mem0's `DEFAULT_UPDATE_MEMORY_PROMPT` (vendored at
`linkedin/vendor/mem0/configs/prompts.py`) to decide whether each new fact
should be added, should update an existing fact, should delete a stale fact, or
is redundant (NONE).

## Conversation Sync

`sync_conversation()` in `linkedin/db/chat.py`:

1. Resolves the conversation URN via `find_conversation_urn()` (API scan) with
   `find_conversation_urn_via_navigation()` fallback
2. Fetches messages via Voyager Messaging GraphQL API
3. Upserts into `ChatMessage` by `linkedin_urn` (dedup key)
4. Folds newly-created rows into `deal.chat_summary` via `update_chat_summary()`

## Message Sending

`send_raw_message()` in `linkedin/actions/message.py` tries three strategies in
order, returning `True` on the first success:

| # | Strategy | Method |
|---|----------|--------|
| 1 | **Popup compose** | Open Message popup on profile page, type, send |
| 2 | **Direct thread** | Navigate to `/messaging/thread/new/?recipient=<urn>`, compose, send |
| 3 | **Voyager API** | REST API call via `api/messaging/send.py` |

Each strategy uses the lead's URN (stored on `Lead.urn`). If all three fail,
`handle_follow_up` reverts the Deal to QUALIFIED for re-connection.

## Scheduling & Deduplication

`enqueue_follow_up(campaign_id, public_id, delay_seconds=10)` in
`linkedin/tasks/scheduler.py`:

- Creates a PENDING `Task` with `scheduled_at = now + delay_seconds`
- **Dedup**: only one FOLLOW_UP task per `(campaign_id, public_id)` exists at a
  time — if one already exists and is pending, it's left untouched

Called from three places:

| Caller | When |
|--------|------|
| `handle_connect()` | profile already CONNECTED (skip connection step) |
| `handle_check_pending()` | connection just accepted (PENDING → CONNECTED) |
| `handle_follow_up()` | self-rescheduling after `send_message` or `wait` |

## Rate Limiting

- Daily limit: `LinkedInProfile.follow_up_daily_limit` (default 30)
- Tracked via `ActionLog` with `action_type=FOLLOW_UP`
- When exhausted: task re-enqueued with **1-hour delay**
- Resets daily; cached in-memory via `LinkedInProfile._exhausted` dict

## Failure Handling

| Failure | Recovery |
|---------|----------|
| Send failed (all 3 strategies) | Deal reverted to QUALIFIED for re-connection |
| Send failed on a `handoff` | Deal still moves to HANDOFF and the alert still fires — a hot lead reaches a human even when LinkedIn refuses the message |
| No Deal found for public_id | Task skipped with warning |
| Deal is not CONNECTED | Task dropped, nothing re-enqueued (checked before the rate limiter) |
| Rate limit exhausted | Task re-enqueued in 1 hour |
| LLM returns unparseable output | `RuntimeError` raised, daemon stops |
| 401 / `AuthenticationError` | Daemon re-authenticates, resets task to pending |
| Handoff spool unwritable | Logged, returns False; the daemon keeps running |

## Handoff

When the lead asks to talk — proposes or accepts a call, asks for price or
terms, hands over a phone or email, asks for a person — the agent picks
`handoff`. The handler sends one short holding sentence, moves the Deal to
`HANDOFF`, spools an alert and **enqueues nothing**. The missing task is the
feature: the bot must not keep writing over the salesperson who takes over.

Idempotency is free rather than bolted on. `HANDOFF` is absent from
`_seed_deal_tasks`'s `active_states`, so `reconcile()` — which re-fires
`on_deal_state_entered` for every active deal on every idle cycle — never
revisits a handed-over deal. One handoff, one alert, forever.

The alert itself is a ready-to-send plain-text file written by
`linkedin/handoff.py` into `OO_HANDOFF_SPOOL_DIR` (default `/tmp/handoff`,
bind-mounted to `/var/openoutreach-tmp/handoff`). The daemon holds no Telegram
credentials — those live on the host, and `fire-monitor.sh:check_handoff()` in
the owlgram repo does the posting. It carries the lead's verbatim last message,
the profile URL, the first profile facts (`Lead` stores no human name) and the
last turns.

A human picks it up in the Django admin: filter Deals by `state=Handoff`. The
"Вернуть боту" action puts the deal back to CONNECTED, and the scheduler hook
re-enqueues the follow_up that HANDOFF withheld.

**Latency.** This is a poll, not a push: a reply is only read when that lead's
own follow_up task fires. While the newest stored message is inbound, the
agent's chosen `follow_up_hours` is capped at `LIVE_CONVERSATION_MAX_HOURS`
(8). This cannot fight `_too_soon_to_nudge`, which only trips when the last
message is ours. An inbox sweep would cut this to minutes and is deliberately
not built yet.

## Prompt Strategy (Mom Test)

The system prompt (`follow_up_agent.j2`) follows the Mom Test method:

- **Discovery first**: open with questions about the lead's work and problems — no product mention until real signal emerges
- **Pitching on signal**: transition when the lead describes a concrete problem we solve, expresses frustration with their current approach, or asks what we do
- **Keep learning while pitching**: weave discovery questions into the conversation even after introducing the product
- **Language**: infer from profile facts (name origin, location, languages); default to English
- **Tone**: short, casual, warm — like real LinkedIn DMs (1-3 sentences max)
- **No boilerplate**: no placeholders, no signatures, no corporate speak
- **Timing**: agent decides — active reply → 2-8h; async → 24h; no reply → 24-48h; 3+ unanswered → consider `mark_completed`
- **Booking link**: include naturally when suggesting a call, not as a standalone line

## CLI Debugging

The agent can be run standalone for debugging:

```bash
# By profile
.venv/bin/python -m linkedin.agents.follow_up --profile john-doe

# By task ID
.venv/bin/python -m linkedin.agents.follow_up --task-id 42
```

Prints the decision (action, message, reason, follow-up hours) without
executing it.
