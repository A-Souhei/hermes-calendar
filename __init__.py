"""Hermes calendar plugin — personal secretary-book calendar with reminders.

Tools (toolset "calendar"):
  calendar_add_event      — create a one-time or recurring event
  calendar_update_event   — modify fields of an existing event
  calendar_remove_event   — delete a series or skip one occurrence
  calendar_list_events    — expand occurrences in a date range
  calendar_get_event      — full event details including next occurrence

Storage: SQLite at $HERMES_HOME/calendar.db
Alerts: fired via Home Assistant (ha_notify or ha_speak channel) on a
        background daemon thread started in register().
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dateutil import parser as dtparser

from . import recurrence as recurrence_mod
from . import scheduler
from . import store
from tools.registry import tool_error, tool_result

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LEAD_RE = re.compile(
    r"(?:(\d+)\s*d(?:ay)?s?)?\s*(?:(\d+)\s*h(?:our)?s?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?\s*(?:(\d+)\s*s(?:ec(?:ond)?s?)?)?",
    re.IGNORECASE,
)

_LEAD_KEYWORDS = {
    "day": 86400, "days": 86400,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
}


def _parse_lead(value: Any) -> Optional[int]:
    """Parse an alert lead into seconds.

    Accepts: int seconds | "1 hour" | "30 minutes" | "2 days" | "90" | None
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    s = str(value).strip().lower()
    if not s:
        return None
    # Pure integer string
    if s.isdigit():
        return int(s)
    # Keyword form: "1 hour", "30 minutes", "2 days 3 hours"
    total = 0
    found = False
    parts = re.split(r"[\s,]+", s)
    i = 0
    while i < len(parts):
        part = parts[i]
        if part.isdigit() and i + 1 < len(parts):
            unit = parts[i + 1].rstrip("s.")
            multiplier = _LEAD_KEYWORDS.get(unit) or _LEAD_KEYWORDS.get(unit + "s")
            if multiplier:
                total += int(part) * multiplier
                found = True
                i += 2
                continue
        elif part.isdigit():
            total += int(part)
            found = True
        i += 1
    return total if found else None


_FREQ_ALIASES: Dict[str, str] = {
    "daily": "daily", "day": "daily", "every day": "daily",
    "weekly": "weekly", "week": "weekly", "every week": "weekly",
    "monthly": "monthly", "month": "monthly", "every month": "monthly",
    "yearly": "yearly", "year": "yearly", "annually": "yearly",
}

_DAY_NAMES: Dict[str, int] = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _parse_recurrence(value: Any) -> Optional[Dict[str, Any]]:
    """Parse a recurrence spec into the canonical dict.

    Accepts:
      None / "" -> None (one-time)
      dict with freq/interval/... -> validated and returned
      str "daily" | "weekly" | "every 2 weeks" | "weekly:mon,wed" | "monthly" | "yearly"
    """
    if value is None or value == "":
        return None

    if isinstance(value, dict):
        freq = str(value.get("freq", "weekly")).lower()
        if freq not in recurrence_mod._FREQ_MAP:
            return None
        result: Dict[str, Any] = {
            "freq": freq,
            "interval": max(1, int(value.get("interval") or 1)),
        }
        bwd = value.get("byweekday")
        if bwd is not None:
            result["byweekday"] = [int(d) for d in bwd if 0 <= int(d) <= 6]
        if value.get("count") is not None:
            result["count"] = int(value["count"])
        if value.get("until") is not None:
            result["until"] = str(value["until"])
        return result

    s = str(value).strip().lower()
    if not s:
        return None

    # "every N weeks" / "every N days" etc.
    interval = 1
    every_m = re.match(r"every\s+(\d+)\s+(\w+)", s)
    if every_m:
        interval = int(every_m.group(1))
        unit_word = every_m.group(2).rstrip("s")
        s = unit_word  # reduce to the base keyword

    # "weekly:mon,wed"
    byweekday = None
    if ":" in s:
        base, days_part = s.split(":", 1)
        s = base.strip()
        day_tokens = [d.strip() for d in days_part.split(",")]
        byweekday = [_DAY_NAMES[d] for d in day_tokens if d in _DAY_NAMES]

    freq = _FREQ_ALIASES.get(s.strip())
    if freq is None:
        return None

    result = {"freq": freq, "interval": interval}
    if byweekday is not None:
        result["byweekday"] = byweekday
    return result


def _check_available() -> bool:
    return True


def _parse_start(raw: Any) -> Optional[datetime]:
    """Parse a start string to an aware datetime.

    The agent must supply an absolute datetime string — dateutil.parser handles
    most ISO and human formats. Returns None on failure.
    """
    if raw is None:
        return None
    try:
        dt = dtparser.parse(str(raw))
        if dt.tzinfo is None:
            # Treat naively as UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _human_recurrence(rec: Optional[Dict]) -> Optional[str]:
    if not rec:
        return None
    freq = rec.get("freq", "weekly")
    interval = rec.get("interval", 1)
    bwd = rec.get("byweekday")
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    if interval == 1:
        label = freq.capitalize()
    else:
        label = f"Every {interval} {freq[:-2] if freq.endswith('ly') else freq}s"

    if bwd:
        label += " on " + ", ".join(day_names[d] for d in bwd if 0 <= d <= 6)
    if rec.get("until"):
        label += f" until {rec['until'][:10]}"
    if rec.get("count"):
        label += f" ({rec['count']} times)"
    return label


def _event_summary(ev: Dict) -> Dict:
    """Return a compact summary suitable for tool responses."""
    return {
        "id": ev["id"],
        "title": ev["title"],
        "start_utc": ev["start_utc"],
        "tz": ev.get("tz"),
        "all_day": ev.get("all_day", False),
        "recurrence": _human_recurrence(ev.get("recurrence")),
        "alert_lead_seconds": ev.get("alert_lead_seconds"),
        "alert_channel": ev.get("alert_channel"),
        "location": ev.get("location"),
        "tags": ev.get("tags"),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_calendar_add_event(args: Dict[str, Any], **kw) -> str:
    title = str(args.get("title") or "").strip()
    if not title:
        return tool_error("title is required")

    start_raw = args.get("start")
    if not start_raw:
        return tool_error(
            "start is required — pass an absolute datetime string, e.g. '2026-06-10T14:00:00+03:00'"
        )
    start_dt = _parse_start(start_raw)
    if start_dt is None:
        return tool_error(f"Could not parse start datetime: {start_raw!r}")

    tz_name = args.get("tz") or recurrence_mod.DEFAULT_TZ
    start_utc = start_dt.astimezone(timezone.utc).isoformat()

    rec = _parse_recurrence(args.get("recurrence"))
    lead = _parse_lead(args.get("alert_lead"))

    channel = str(args.get("alert_channel") or "ha_notify").strip().lower()
    if channel not in ("ha_notify", "ha_speak", "none"):
        channel = "ha_notify"

    meeting_raw = args.get("meeting")
    meeting = None
    if isinstance(meeting_raw, dict):
        meeting = {
            "participants": meeting_raw.get("participants") or [],
            "room_url": meeting_raw.get("room_url"),
            "room_app": meeting_raw.get("room_app"),
        }

    tags_raw = args.get("tags")
    tags: Optional[List[str]] = None
    if isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw]

    d = {
        "title": title,
        "description": args.get("description"),
        "start_utc": start_utc,
        "tz": tz_name,
        "all_day": bool(args.get("all_day", False)),
        "recurrence": rec,
        "alert_lead_seconds": lead,
        "alert_channel": channel,
        "meeting": meeting,
        "location": args.get("location"),
        "tags": tags,
    }
    try:
        event_id = store.add_event(d)
    except Exception as e:
        logger.exception("calendar_add_event store error")
        return tool_error(f"Failed to save event: {e}")

    ev = store.get_event(event_id)
    return tool_result({"created": True, **_event_summary(ev)})


def _handle_calendar_update_event(args: Dict[str, Any], **kw) -> str:
    event_id = str(args.get("id") or "").strip()
    if not event_id:
        return tool_error("id is required")

    ev = store.get_event(event_id)
    if ev is None:
        return tool_error(f"Event not found: {event_id}")

    fields: Dict[str, Any] = {}

    if "title" in args and args["title"] is not None:
        title = str(args["title"]).strip()
        if not title:
            return tool_error("title cannot be empty")
        fields["title"] = title

    if "description" in args:
        fields["description"] = args["description"]

    if "start" in args and args["start"] is not None:
        start_dt = _parse_start(args["start"])
        if start_dt is None:
            return tool_error(f"Could not parse start: {args['start']!r}")
        fields["start_utc"] = start_dt.astimezone(timezone.utc).isoformat()

    if "tz" in args and args["tz"] is not None:
        fields["tz"] = str(args["tz"])

    if "all_day" in args:
        fields["all_day"] = bool(args["all_day"])

    if "recurrence" in args:
        fields["recurrence"] = _parse_recurrence(args["recurrence"])

    if "alert_lead" in args:
        fields["alert_lead_seconds"] = _parse_lead(args["alert_lead"])

    if "alert_channel" in args and args["alert_channel"] is not None:
        ch = str(args["alert_channel"]).strip().lower()
        fields["alert_channel"] = ch if ch in ("ha_notify", "ha_speak", "none") else "ha_notify"

    if "meeting" in args:
        meeting_raw = args["meeting"]
        if isinstance(meeting_raw, dict):
            fields["meeting"] = {
                "participants": meeting_raw.get("participants") or [],
                "room_url": meeting_raw.get("room_url"),
                "room_app": meeting_raw.get("room_app"),
            }
        else:
            fields["meeting"] = None

    if "location" in args:
        fields["location"] = args["location"]

    if "tags" in args:
        tags_raw = args["tags"]
        fields["tags"] = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else None

    if not fields:
        return tool_error("No updatable fields provided")

    try:
        updated = store.update_event(event_id, fields)
    except Exception as e:
        logger.exception("calendar_update_event store error")
        return tool_error(f"Failed to update event: {e}")

    if not updated:
        return tool_error(f"Event not found: {event_id}")

    ev = store.get_event(event_id)
    return tool_result({"updated": True, **_event_summary(ev)})


def _handle_calendar_remove_event(args: Dict[str, Any], **kw) -> str:
    event_id = str(args.get("id") or "").strip()
    if not event_id:
        return tool_error("id is required")

    scope = str(args.get("scope") or "all").strip().lower()
    occurrence_raw = args.get("occurrence")

    if scope == "occurrence":
        if not occurrence_raw:
            return tool_error("occurrence date is required when scope is 'occurrence'")
        occ_dt = _parse_start(occurrence_raw)
        if occ_dt is None:
            return tool_error(f"Could not parse occurrence: {occurrence_raw!r}")
        occ_iso = occ_dt.astimezone(timezone.utc).isoformat()
        try:
            store.add_exception(event_id, occ_iso)
        except Exception as e:
            return tool_error(f"Failed to skip occurrence: {e}")
        return tool_result({"skipped_occurrence": occ_iso, "event_id": event_id})

    # scope == "all" (default)
    try:
        removed = store.remove_event(event_id)
    except Exception as e:
        logger.exception("calendar_remove_event store error")
        return tool_error(f"Failed to remove event: {e}")

    if not removed:
        return tool_error(f"Event not found: {event_id}")
    return tool_result({"removed": True, "event_id": event_id})


def _handle_calendar_list_events(args: Dict[str, Any], **kw) -> str:
    now_utc = datetime.now(timezone.utc)

    from_raw = args.get("from")
    to_raw = args.get("to")

    range_start = _parse_start(from_raw) if from_raw else now_utc
    if range_start is None:
        return tool_error(f"Could not parse 'from': {from_raw!r}")

    range_end = _parse_start(to_raw) if to_raw else now_utc + timedelta(days=30)
    if range_end is None:
        return tool_error(f"Could not parse 'to': {to_raw!r}")

    if range_start > range_end:
        return tool_error("'from' must be before 'to'")

    query = str(args.get("query") or "").strip().lower()

    try:
        events = store.list_events()
    except Exception as e:
        return tool_error(f"Failed to list events: {e}")

    items = []
    from zoneinfo import ZoneInfo

    for ev in events:
        # Apply text filter before expanding (title, description, tags)
        if query:
            haystack = " ".join(filter(None, [
                ev.get("title", ""),
                ev.get("description", ""),
                " ".join(ev.get("tags") or []),
            ])).lower()
            if query not in haystack:
                continue

        ev_copy = dict(ev)
        try:
            ev_copy["_exceptions"] = store.get_exceptions(ev["id"])
        except Exception:
            ev_copy["_exceptions"] = set()

        try:
            occs = recurrence_mod.occurrences(ev_copy, range_start, range_end)
        except Exception as e:
            logger.warning("calendar_list_events: recurrence error for %s: %s", ev["id"], e)
            continue

        tz_name = ev.get("tz") or recurrence_mod.DEFAULT_TZ
        try:
            event_tz = ZoneInfo(tz_name)
        except Exception:
            event_tz = ZoneInfo(recurrence_mod.DEFAULT_TZ)

        is_recurring = bool(ev.get("recurrence"))
        for occ_utc in occs:
            occ_local = occ_utc.astimezone(event_tz)
            items.append({
                "id": ev["id"],
                "title": ev["title"],
                "occurrence_local": occ_local.isoformat(),
                "occurrence_utc": occ_utc.isoformat(),
                "recurring": is_recurring,
                "all_day": ev.get("all_day", False),
                "alert_channel": ev.get("alert_channel"),
                "location": ev.get("location"),
                "tags": ev.get("tags"),
            })

    items.sort(key=lambda x: x["occurrence_utc"])
    return tool_result({"count": len(items), "from": range_start.isoformat(),
                        "to": range_end.isoformat(), "events": items})


def _handle_calendar_get_event(args: Dict[str, Any], **kw) -> str:
    event_id = str(args.get("id") or "").strip()
    if not event_id:
        return tool_error("id is required")

    ev = store.get_event(event_id)
    if ev is None:
        return tool_error(f"Event not found: {event_id}")

    now_utc = datetime.now(timezone.utc)
    try:
        next_occ = recurrence_mod.next_occurrence(ev, now_utc)
        next_occ_iso = next_occ.isoformat() if next_occ else None
    except Exception:
        next_occ_iso = None

    exceptions = []
    try:
        exceptions = sorted(store.get_exceptions(event_id))
    except Exception:
        pass

    return tool_result({
        "id": ev["id"],
        "title": ev["title"],
        "description": ev.get("description"),
        "start_utc": ev["start_utc"],
        "tz": ev.get("tz"),
        "all_day": ev.get("all_day", False),
        "recurrence": ev.get("recurrence"),
        "recurrence_human": _human_recurrence(ev.get("recurrence")),
        "alert_lead_seconds": ev.get("alert_lead_seconds"),
        "alert_channel": ev.get("alert_channel"),
        "meeting": ev.get("meeting"),
        "location": ev.get("location"),
        "tags": ev.get("tags"),
        "next_occurrence_utc": next_occ_iso,
        "skipped_occurrences": exceptions,
        "created_utc": ev.get("created_utc"),
        "updated_utc": ev.get("updated_utc"),
    })


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_RECURRENCE_DESCRIPTION = (
    "Recurrence pattern. Examples: 'daily', 'weekly', 'every 2 weeks', "
    "'weekly:mon,wed,fri', 'monthly', 'yearly'. "
    "Or a structured object: {\"freq\": \"weekly\", \"interval\": 2, "
    "\"byweekday\": [0, 2], \"count\": 10, \"until\": \"2027-01-01T00:00:00Z\"}. "
    "byweekday uses 0=Mon … 6=Sun. Omit or set null for a one-time event."
)

_ALERT_LEAD_DESCRIPTION = (
    "How far before the event to fire the reminder. "
    "Examples: '1 hour', '30 minutes', '2 days', 90 (seconds). "
    "Null = use defaults (1 hour before timed events; 9 AM on the event day for all-day events)."
)

_START_DESCRIPTION = (
    "Absolute datetime for the event start. You know the current date — resolve relative "
    "expressions ('tomorrow', 'next Monday') to an ISO string before calling. "
    "Example: '2026-06-15T09:00:00+03:00'. All-day events can use 'YYYY-MM-DD'."
)

CALENDAR_ADD_EVENT_SCHEMA = {
    "name": "calendar_add_event",
    "description": (
        "Add a new event to the calendar. Supports one-time and recurring events, "
        "meeting details (participants, video room URL/app), location, tags, and "
        "configurable reminders via Home Assistant push or TTS."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Event title (required)."},
            "start": {"type": "string", "description": _START_DESCRIPTION},
            "description": {"type": "string", "description": "Optional free-text description."},
            "all_day": {"type": "boolean", "description": "True for all-day events (no specific time)."},
            "tz": {
                "type": "string",
                "description": (
                    "IANA timezone for the event, e.g. 'Indian/Antananarivo', 'Europe/Paris'. "
                    f"Defaults to {recurrence_mod.DEFAULT_TZ}."
                ),
            },
            "recurrence": {"description": _RECURRENCE_DESCRIPTION},
            "alert_lead": {"description": _ALERT_LEAD_DESCRIPTION},
            "alert_channel": {
                "type": "string",
                "enum": ["ha_notify", "ha_speak", "none"],
                "description": "Delivery channel for reminders (default: ha_notify).",
            },
            "meeting": {
                "type": "object",
                "description": "Optional meeting details.",
                "properties": {
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Participant names or emails.",
                    },
                    "room_url": {"type": "string", "description": "Video room URL (Zoom, Meet, etc.)."},
                    "room_app": {"type": "string", "description": "App name, e.g. 'Zoom', 'Google Meet'."},
                },
            },
            "location": {"type": "string", "description": "Physical location or address."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Free-form tags for filtering, e.g. ['work', 'health'].",
            },
        },
        "required": ["title", "start"],
    },
}

CALENDAR_UPDATE_EVENT_SCHEMA = {
    "name": "calendar_update_event",
    "description": (
        "Update one or more fields of an existing calendar event. "
        "Only provided fields are changed; others are left as-is. "
        "Updating recurrence replaces the entire recurrence rule."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Event ID (from calendar_add_event or calendar_list_events)."},
            "title": {"type": "string"},
            "start": {"type": "string", "description": _START_DESCRIPTION},
            "description": {"type": "string"},
            "all_day": {"type": "boolean"},
            "tz": {"type": "string"},
            "recurrence": {"description": _RECURRENCE_DESCRIPTION},
            "alert_lead": {"description": _ALERT_LEAD_DESCRIPTION},
            "alert_channel": {
                "type": "string",
                "enum": ["ha_notify", "ha_speak", "none"],
            },
            "meeting": {
                "type": "object",
                "properties": {
                    "participants": {"type": "array", "items": {"type": "string"}},
                    "room_url": {"type": "string"},
                    "room_app": {"type": "string"},
                },
            },
            "location": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id"],
    },
}

CALENDAR_REMOVE_EVENT_SCHEMA = {
    "name": "calendar_remove_event",
    "description": (
        "Remove a calendar event. Use scope='all' (default) to delete the entire series, "
        "or scope='occurrence' + occurrence date to skip a single occurrence of a recurring event."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Event ID."},
            "scope": {
                "type": "string",
                "enum": ["all", "occurrence"],
                "description": "'all' removes the entire event/series. 'occurrence' skips one date.",
            },
            "occurrence": {
                "type": "string",
                "description": (
                    "Required when scope='occurrence'. The datetime of the specific occurrence "
                    "to skip, e.g. '2026-07-14T09:00:00+03:00'."
                ),
            },
        },
        "required": ["id"],
    },
}

CALENDAR_LIST_EVENTS_SCHEMA = {
    "name": "calendar_list_events",
    "description": (
        "List calendar events expanded into individual occurrences within a date range. "
        "Recurring events appear once per occurrence in the range. "
        "Defaults to now … now+30 days."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "from": {
                "type": "string",
                "description": "Range start (ISO datetime, default: now).",
            },
            "to": {
                "type": "string",
                "description": "Range end (ISO datetime, default: now + 30 days).",
            },
            "query": {
                "type": "string",
                "description": "Substring filter applied to title, description, and tags.",
            },
        },
    },
}

CALENDAR_GET_EVENT_SCHEMA = {
    "name": "calendar_get_event",
    "description": (
        "Get full details of a calendar event: description, recurrence rule (human-readable), "
        "alert configuration, meeting info (participants, room URL/app), "
        "next scheduled occurrence, and any skipped occurrences."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Event ID."},
        },
        "required": ["id"],
    },
}


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

_TOOLS = (
    ("calendar_add_event",    CALENDAR_ADD_EVENT_SCHEMA,    _handle_calendar_add_event,    "📅"),
    ("calendar_update_event", CALENDAR_UPDATE_EVENT_SCHEMA, _handle_calendar_update_event, "✏️"),
    ("calendar_remove_event", CALENDAR_REMOVE_EVENT_SCHEMA, _handle_calendar_remove_event, "🗑️"),
    ("calendar_list_events",  CALENDAR_LIST_EVENTS_SCHEMA,  _handle_calendar_list_events,  "📋"),
    ("calendar_get_event",    CALENDAR_GET_EVENT_SCHEMA,    _handle_calendar_get_event,    "🔍"),
)


def register(ctx) -> None:
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="calendar",
            schema=schema,
            handler=handler,
            check_fn=_check_available,
            emoji=emoji,
        )

    # Start the background alert scheduler after Hermes is fully initialised
    if hasattr(ctx, "on_ready"):
        ctx.on_ready(scheduler.start)
    else:
        scheduler.start()
