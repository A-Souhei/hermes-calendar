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

## Storage

SQLite at `$HERMES_HOME/calendar.db` (default `~/.hermes/calendar.db`), WAL mode.

## Configuration

Optional `~/.hermes/calendar_config.json`:

```json
{
  "default_lead_seconds": 3600,
  "daily_alert_hour": 9,
  "check_interval_seconds": 60,
  "boot_catchup_seconds": 7200
}
```

HA connection uses the same env vars and `~/.hermes/ha_notify.json` override file
as the `ha_notify` plugin (`HASS_URL`/`HA_URL`, `HASS_TOKEN`/`HA_TOKEN`,
`HA_NOTIFY_TARGET`). The event timezone defaults to `CALENDAR_TZ` env var or
`Indian/Antananarivo`.
