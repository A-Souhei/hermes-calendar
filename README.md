# hermes-calendar

Personal secretary-book calendar for Hermes — one-time & recurring events with
multi-channel reminders, meeting reports, work timers with job/category
time-tracking, and period plannings with emailed completion reports. All editing
is through the agent (CRUD via tools); a dashboard tab adds read-only
Calendar/Plannings views plus one-click timer resume/stop. Owners are
**pre-registered** (see [User registry](#user-registry)).

## Tools

**Events**

| Tool | Description |
|---|---|
| `calendar_add_event` | Create an event (one-time or recurring). Pass an absolute datetime for `start`. |
| `calendar_update_event` | Update any field of an existing event by ID. |
| `calendar_remove_event` | Delete a series (`scope=all`) or skip one occurrence (`scope=occurrence`). |
| `calendar_list_events` | Expand occurrences in a date range; optional substring filter. Defaults to **start of today → now+30d**. |
| `calendar_get_event` | Full details: recurrence, alert config, meeting, next occurrence. |

**Reports & status**

| Tool | Description |
|---|---|
| `calendar_set_report` | Attach/update a **report** for one occurrence — minutes, transcription, attendees, decisions, outcome. One-time *and* recurring (pass `occurrence`). Fields merge. |
| `calendar_get_report` / `calendar_list_reports` | Read one occurrence's report / all reports for an event. |
| `calendar_set_status` | Mark an occurrence `confirmed` (it happened), `missed`, or `floating` (reset to unknown). |

**Timers & jobs** — see [Realtime jobs & time tracking](#realtime-jobs--time-tracking)

| Tool | Description |
|---|---|
| `calendar_start_timer` | Start a work timer now (open-ended or fixed `duration`); tag it with a `job`/`category`. |
| `calendar_stop_timer` | Stop the running timer and record the measured duration. |
| `calendar_resume_job` | Start a fresh session of an existing job, reusing its exact name + category so sessions aggregate. |
| `calendar_list_jobs` | List distinct jobs for an owner with total time and session counts. |
| `calendar_job_summary` | Aggregate tracked time by job/category for a period; optionally email a styled report (HTML + PDF). |

**Plannings** — see [Plannings](#plannings)

| Tool | Description |
|---|---|
| `calendar_create_planning` | Create a named, period-bounded set of objectives (events) scored by completion. |
| `calendar_list_plannings` / `calendar_get_planning` | List plannings (with overall stats) / one planning's details. |
| `calendar_planning_report` | Compute + (optionally) email the completion report for a planning. |
| `calendar_remove_planning` | Delete a planning (optionally its events too). |

**Users & digest**

| Tool | Description |
|---|---|
| `calendar_set_user_email` / `calendar_list_user_emails` | Associate a person with an email (for email-channel reminders/reports) / list associations. |
| `calendar_digest` | Build (and optionally email) a per-owner daily digest. |

Reports are stored per `(event, occurrence)`, so each instance of a recurring event (e.g. every weekly standup) keeps its own minutes/transcription, and one-off meetings get a report too.

## Alert defaults

| Situation | Default behaviour |
|---|---|
| Timed event, no `alert_lead` | Reminder fires **1 hour before** the event |
| All-day event, no `alert_lead` | Reminder fires at **09:00** on the event day (in the event's timezone) |
| Per-event override | Set `alert_lead` e.g. `"2 days"`, `"30 minutes"`, or seconds as integer |
| Silence reminders | Set `alert_channel` to `"none"` |

## Channels

`alert_channel` is a logical value expanded into concrete delivery channels:

- `ha_notify` (default) — Home Assistant push notification (title + message body)
- `ha_speak` — TTS spoken on the phone
- `chat` — a message into the Hermes chat (delivered via the every-minute cron tick's stdout)
- `email` — emailed reminder (only to an address in the `EMAIL_ALLOWED_USERS` allowlist)
- `both` — `ha_notify` + `ha_speak`
- `all` — `ha_notify` + `ha_speak` + `chat` + `email`
- `none` — silence reminders for the event

## Alert engine

Reminders fire from an **every-minute cron tick** — reliable, independent of
agent activity (the gateway loads plugins lazily, so the in-gateway thread alone
is not enough). Put `calendar_tick.py` in `~/.hermes/scripts/` and:

```
hermes cron create "* * * * *" --name calendar-alerts --no-agent --script calendar_tick.py
```

`calendar_tick.py` fires due reminders via Home Assistant and prints nothing
(so cron delivers nothing to chat — alerts go to your phone). It looks back to
the last tick (capped at `max_catchup_seconds`) so brief downtime still catches
up; `fired_alerts` dedup guarantees each alert fires once. The in-gateway thread
is a bonus for sub-minute responsiveness when the agent is active.

## Daily digest

A per-owner daily digest summarises the calendar events that need attention
today, delivered as a styled HTML email with a plain-text fallback.

**Always delivered.** When an owner has no events today the digest falls back
to showing their single closest upcoming event, so the email is never empty.

**Cron setup** — place `calendar_digest.py` in `~/.hermes/scripts/` and run:

```
hermes cron create "0 7 * * *" --name calendar-digest --no-agent \
    --script calendar_digest.py
```

**Delivery:**
- Owners with a registered email address that is in the `EMAIL_ALLOWED_USERS`
  allowlist receive the digest by email.
- Owners without a usable email have their digest printed to stdout, so the
  `--no-agent` cron posts it into the Hermes chat.

**On-demand tool:** `calendar_digest` builds (and optionally emails) the digest
for a given owner from within the agent conversation.

## Plannings

A **planning** is a named, period-bounded set of objectives (each objective is a
calendar event tagged with the planning). Completion is scored per occurrence
from the per-occurrence status: **only `confirmed` counts as done** — everything
else (unconfirmed/floating, missed, active) counts as not done.

- Create with `calendar_create_planning` (compute `period_start`/`period_end`
  yourself — `period_end` is **exclusive**), then attach events via the
  `planning` param of `calendar_add_event`.
- The owner **must have a registered email** — reports are emailed (the chat only
  announces a report is ready).
- A report is **auto-emailed once at 09:00** the morning after the period ends,
  and can be produced on demand with `calendar_planning_report`. It includes
  overall + per-objective completion stats and a styled **PDF** attachment
  (falls back to text-only if `weasyprint` is missing).
- Plannings appear as a second dashboard tab with overall completion stats.

## User registry

All calendar owners must be pre-registered in `~/.hermes/calendar-users.json`
(or the path set by `$CALENDAR_USERS_FILE`). Creation tools
(`calendar_add_event`, `calendar_start_timer`, `calendar_resume_job`,
`calendar_create_planning`) **refuse unregistered owners** with a message
naming the fix. The agent must never create a user on the fly — add them to the
file first.

**File format** — either a bare list or a `{"users": [...]}` wrapper; each
entry is a string (name only) or a rich object:

```json
{
  "users": [
    { "name": "Alice",   "email": "alice@example.com", "language": "en" },
    { "name": "Bob",     "email": "bob@example.com",   "language": "fr" },
    { "name": "Charlie" }
  ]
}
```

Fields per entry:
- `name` (**required**) — the owner identifier used in events.
- `email` (optional) — also used as the email fallback if no `user_emails` DB
  row exists for the person. The `user_emails` table (set via
  `calendar_set_user_email`) always wins when both are present.
- `language` (optional) — `"en"` or `"fr"`.

Edits are picked up without restart (file mtime is checked on each call). A
missing file or parse error returns an empty registry (all owners refused).

## Realtime jobs & time tracking

### One timer per user

Only one timer can run at a time per user. Starting a new timer with
`calendar_start_timer` while one is already running **auto-stops** the previous
one (marks it confirmed, records the measured duration) and starts the new one.
The result includes a `warning` and `switched_from` list describing what was
stopped.

### Job and category attributes

- `job` — applies to **timer events only** (`calendar_start_timer`). A free-text
  work-stream identifier, e.g. `"client-acme"`, `"thesis-writing"`. Use the same
  string across sessions to accumulate time.
- `category` — applies to **all events** (`calendar_start_timer`,
  `calendar_add_event`, `calendar_update_event`). Optional free-text grouping,
  e.g. `"work"`, `"personal"`.

### Resuming a job

`calendar_start_timer` always creates a **new** event, so "resuming" a job means
logging a fresh session tagged with the same `job` string — all sessions sharing
that string aggregate in reports. Use **`calendar_resume_job`** to do this safely:
it looks up the most recent session of a named job, reuses its **exact** stored
`job` name and `category` (so a typo can't fork the job), and starts a new
session (auto-stopping any running timer). If the name doesn't match an existing
job it returns the list of known jobs and asks for the exact one rather than
silently creating a near-duplicate.

### New tools

| Tool | Description |
|---|---|
| `calendar_resume_job` | Start a fresh session of an existing job, reusing its exact name + category so sessions aggregate. |
| `calendar_list_jobs` | List distinct jobs for an owner with total time and session counts. |
| `calendar_job_summary` | Aggregate tracked time by job/category for a period; optionally email a styled report (HTML + PDF). |

**Example phrasings:**
- "Start a timer for client-acme now" → `calendar_start_timer` with `job="client-acme"`
- "Resume the thesis-writing job" → `calendar_resume_job` with `job="thesis-writing"`
- "Stop my timer" → `calendar_stop_timer`
- "How many hours did I spend on thesis-writing this month?" → `calendar_job_summary` with `period="monthly"`
- "Email me my job report for this week" → `calendar_job_summary` with `period="weekly"` and `email=true`

## Dashboard

A dashboard tab (Calendar | Plannings) renders the calendar. It is read-only
except for two timer actions (resume/stop) described below.

**Calendar layout** — a compact **month picker** (left) + a **day agenda**
(right). Click a day to populate the agenda.

- **Today** = a distinct number colour (no filled circle).
- **Days with events** = a **yellow circular background** (neutral "has events").
- **Agenda rows** show `time · title (+ logged duration)` plus a badge line:
  **status** (`confirmed` / `running` / `missed` / `upcoming`), **category**,
  **job**, location. Title colour mirrors the type — **green** regular, **blue**
  job.
- A live **"Running now" banner** names the currently-running session with a
  ticking clock (polls `/timers`, owner-aware); **stat cards** (Events /
  Confirmed / Missed / Upcoming / Active); and **user + category filters** (the
  user filter is registry-driven, so registered users appear even with no events).

**Timer actions (the only writes)** — a job event's detail modal shows:

- **▶ Resume job** (when the session is stopped) → `POST /jobs/resume`, starting a
  new session with the same job + category (auto-stopping any running timer).
- **■ Stop** (when the session is running) → `POST /jobs/stop`, recording the
  measured duration; the modal then flips to a Resume button.

Both endpoints sit behind the dashboard's session-token auth middleware (same as
the other plugins' write routes); the shared timer logic lives in `timers.py` so
the tools and the dashboard cannot drift.

## Notes

Notes are alertless calendar entries for capturing quick thoughts and recalling them later ("what was that thing I noted last week?").

- **Create** with `calendar_add_note` — supply `content` (the note text) and `owner`. Optional: `details` (longer body), `tags`, `when` (defaults to now), `language`.
- **Recall** with `calendar_list_notes` — returns notes most-recent-first. Supports `query` (substring over content/details/tags) and `from`/`to` date-range bounds over the note's timestamp.
- Notes are **never alerted** (`alert_channel=none`) and **never appear** in the agenda, calendar grid, or daily digest. They share the same SQLite `events` table, distinguished by `kind='note'`.

## Storage

SQLite at `$HERMES_HOME/calendar.db` (default `~/.hermes/calendar.db`), WAL mode.

## Configuration

Optional `~/.hermes/calendar_config.json`:

```json
{
  "default_lead_seconds": 3600,
  "daily_alert_hour": 9,
  "check_interval_seconds": 60,
  "boot_catchup_seconds": 7200,
  "lookback_seconds": 180,
  "max_catchup_seconds": 21600
}
```

HA connection uses the same env vars and `~/.hermes/ha_notify.json` override file
as the `ha_notify` plugin (`HASS_URL`/`HA_URL`, `HASS_TOKEN`/`HA_TOKEN`,
`HA_NOTIFY_TARGET`). The event timezone defaults to `CALENDAR_TZ` env var or
`Indian/Antananarivo`.
