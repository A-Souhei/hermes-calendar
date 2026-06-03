# hermes-calendar

Personal secretary-book calendar for Hermes. One-time and recurring events with
Home Assistant reminders. All editing is through the agent; a read-only dashboard
is added separately.

## Tools

| Tool | Description |
|---|---|
| `calendar_add_event` | Create an event (one-time or recurring). Pass an absolute datetime for `start`. |
| `calendar_update_event` | Update any field of an existing event by ID. |
| `calendar_remove_event` | Delete a series (`scope=all`) or skip one occurrence (`scope=occurrence`). |
| `calendar_list_events` | Expand occurrences in a date range; optional substring filter. |
| `calendar_get_event` | Full details: recurrence, alert config, meeting, next occurrence. |
| `calendar_set_report` | Attach/update a **report** for one occurrence — minutes, transcription, attendees, decisions, outcome (a meeting/visio that happened). One-time *and* recurring (pass `occurrence` for recurring). Fields merge. |
| `calendar_get_report` | Read the report for a given occurrence. |
| `calendar_list_reports` | All reports for an event across its occurrences. |

Reports are stored per `(event, occurrence)`, so each instance of a recurring event (e.g. every weekly standup) keeps its own minutes/transcription, and one-off meetings get a report too.

## Alert defaults

| Situation | Default behaviour |
|---|---|
| Timed event, no `alert_lead` | Reminder fires **1 hour before** the event |
| All-day event, no `alert_lead` | Reminder fires at **09:00** on the event day (in the event's timezone) |
| Per-event override | Set `alert_lead` e.g. `"2 days"`, `"30 minutes"`, or seconds as integer |
| Silence reminders | Set `alert_channel` to `"none"` |

## Channels

- `ha_notify` (default) — push notification (title + message body)
- `ha_speak` — TTS spoken on phone

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

### Dashboard

Job/timer events render with a **blue accent and bold blue title** in the
read-only Calendar tab, while regular appointments read in **green** —
distinguishing logged work from calendar events at a glance. The event's `job`
and `category` appear in the chip tooltip.

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
