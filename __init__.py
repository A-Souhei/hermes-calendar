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


def _parse_start(raw: Any, tz_name: str) -> Optional[datetime]:
    """Parse a start string to an aware datetime.

    The agent supplies an absolute datetime string — dateutil.parser handles
    most ISO and human formats. A NAIVE datetime (no offset, e.g. "2026-06-05
    15:00") is interpreted in the event's LOCAL timezone (tz_name) — so "3pm"
    means 3pm where the user is, not UTC. Callers convert to UTC for storage.
    Returns None on failure.
    """
    if raw is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        dt = dtparser.parse(str(raw))
        if dt.tzinfo is None:
            try:
                dt = dt.replace(tzinfo=ZoneInfo(tz_name))
            except Exception:
                dt = dt.replace(tzinfo=ZoneInfo(recurrence_mod.DEFAULT_TZ))
        return dt
    except Exception:
        return None


_STATUS_ENUM = ["confirmed", "missed", "floating"]

_CHANNEL_ENUM = ["ha_notify", "ha_speak", "both", "chat", "all", "none"]
_VALID_CHANNELS = set(_CHANNEL_ENUM)
_CHANNEL_DESCRIPTION = (
    "Delivery channel for reminders (default: ha_notify). "
    "'ha_notify' = phone push; 'ha_speak' = spoken TTS on the phone; "
    "'both' = push + speak; 'chat' = a text from Calypso in this chat; "
    "'all' = push + speak + chat; 'none' = no reminder."
)


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
    tz_name = args.get("tz") or recurrence_mod.DEFAULT_TZ
    start_dt = _parse_start(start_raw, tz_name)
    if start_dt is None:
        return tool_error(f"Could not parse start datetime: {start_raw!r}")

    start_utc = start_dt.astimezone(timezone.utc).isoformat()

    rec = _parse_recurrence(args.get("recurrence"))
    lead = _parse_lead(args.get("alert_lead"))

    channel = str(args.get("alert_channel") or "ha_notify").strip().lower()
    if channel not in _VALID_CHANNELS:
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
        start_tz = args.get("tz") or ev.get("tz") or recurrence_mod.DEFAULT_TZ
        start_dt = _parse_start(args["start"], start_tz)
        if start_dt is None:
            return tool_error(f"Could not parse start: {args['start']!r}")
        fields["start_utc"] = start_dt.astimezone(timezone.utc).isoformat()

    if "tz" in args and args["tz"] is not None:
        fields["tz"] = str(args["tz"])

    if "all_day" in args:
        fields["all_day"] = bool(args["all_day"])

    if "recurrence" in args:
        fields["recurrence"] = _parse_recurrence(args["recurrence"])

    # Convenience: merge an end-date into the EXISTING recurrence rule without
    # restating it, so freq/byweekday/interval are never lost. Operates on a
    # recurrence also being set this call, else the stored one.
    if "until" in args:
        base = fields.get("recurrence")
        if base is None:
            base = ev.get("recurrence")
        if not base:
            return tool_error(
                "`until` only applies to a recurring event — this one has no "
                "recurrence rule. Set a recurrence first, or pass a full "
                "`recurrence` that includes 'until'."
            )
        base = dict(base)
        raw_until = args["until"]
        if raw_until is None or str(raw_until).strip().lower() in (
            "never", "none", "", "forever", "no end", "indefinite",
        ):
            base.pop("until", None)
        else:
            # Use the EFFECTIVE tz — if this same call also changes tz, a naive
            # date must be read in the new zone, not the old one.
            eff_tz = fields.get("tz") or ev.get("tz") or recurrence_mod.DEFAULT_TZ
            until_dt = _parse_start(raw_until, eff_tz)
            if until_dt is None:
                return tool_error(f"Could not parse until date: {raw_until!r}")
            base["until"] = until_dt.astimezone(timezone.utc).isoformat()
        fields["recurrence"] = base

    if "alert_lead" in args:
        fields["alert_lead_seconds"] = _parse_lead(args["alert_lead"])

    if "alert_channel" in args and args["alert_channel"] is not None:
        ch = str(args["alert_channel"]).strip().lower()
        fields["alert_channel"] = ch if ch in _VALID_CHANNELS else "ha_notify"

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
        ev_occ = store.get_event(event_id)
        if ev_occ is None:
            return tool_error(f"Event not found: {event_id}")
        # Snap the given date to the real occurrence on that local day so the
        # exception actually matches the series' occurrence instant.
        occ_iso = _resolve_occurrence(ev_occ, occurrence_raw)
        if occ_iso is None:
            return tool_error(f"Could not resolve occurrence: {occurrence_raw!r}")
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
            occ_iso = occ_utc.isoformat()
            status_row = store.get_status(ev["id"], occ_iso)
            items.append({
                "id": ev["id"],
                "title": ev["title"],
                "occurrence_local": occ_local.isoformat(),
                "occurrence_utc": occ_iso,
                "recurring": is_recurring,
                "all_day": ev.get("all_day", False),
                "alert_channel": ev.get("alert_channel"),
                "location": ev.get("location"),
                "tags": ev.get("tags"),
                "status": status_row["status"] if status_row else "floating",
                "duration_seconds": status_row.get("duration_seconds") if status_row else None,
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

    statuses = []
    try:
        statuses = store.list_statuses(event_id)
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
        "statuses": statuses,
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
                "enum": _CHANNEL_ENUM,
                "description": _CHANNEL_DESCRIPTION,
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
            "until": {
                "type": ["string", "null"],
                "description": (
                    "End-date for a recurring series. Merges an 'until' cutoff "
                    "into the EXISTING recurrence rule without restating it "
                    "(freq/byweekday/interval are preserved) — use this to stop "
                    "a series on a date, e.g. '2026-12-31'. Pass 'never' or null "
                    "to remove an existing end date. Only valid for recurring events."
                ),
            },
            "alert_lead": {"description": _ALERT_LEAD_DESCRIPTION},
            "alert_channel": {
                "type": "string",
                "enum": _CHANNEL_ENUM,
                "description": _CHANNEL_DESCRIPTION,
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
# Reports — per-occurrence minutes / transcription / notes (one-time & recurring)
# ---------------------------------------------------------------------------

_REPORT_TEXT_FIELDS = ("minutes", "transcription", "summary", "outcome", "notes")
_REPORT_LIST_FIELDS = ("attendees", "action_items", "decisions", "links", "tags")


def _resolve_occurrence(ev: Dict[str, Any], occurrence_arg: Any) -> Optional[str]:
    """Resolve the UTC-iso occurrence key a report attaches to.

    one-time + no occurrence -> the event's own start; occurrence given -> snap
    to the real occurrence on that local day if any, else the provided instant;
    recurring + no occurrence -> None (caller must supply a date).
    """
    tz_name = ev.get("tz") or recurrence_mod.DEFAULT_TZ
    if not occurrence_arg:
        if not ev.get("recurrence"):
            return ev["start_utc"]
        return None
    dt = _parse_start(occurrence_arg, tz_name)
    if dt is None:
        return None
    occ_utc = dt.astimezone(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        etz = ZoneInfo(tz_name)
        local = occ_utc.astimezone(etz)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        occs = recurrence_mod.occurrences(
            ev, day_start.astimezone(timezone.utc), day_end.astimezone(timezone.utc)
        )
        if occs:
            return occs[0].astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    return occ_utc.isoformat()


def _build_report(args: Dict[str, Any]) -> Dict[str, Any]:
    report: Dict[str, Any] = {}
    for k in _REPORT_TEXT_FIELDS:
        v = args.get(k)
        if v is not None and str(v).strip():
            report[k] = v
    for k in _REPORT_LIST_FIELDS:
        v = args.get(k)
        if v is not None:
            report[k] = v if isinstance(v, list) else [v]
    return report


def _report_event_id(args: Dict[str, Any]) -> str:
    return str(args.get("id") or args.get("event_id") or "").strip()


def _handle_calendar_set_report(args: Dict[str, Any], **kw) -> str:
    event_id = _report_event_id(args)
    if not event_id:
        return tool_error("id (the event id) is required")
    ev = store.get_event(event_id)
    if ev is None:
        return tool_error(f"Event not found: {event_id}")
    occ = _resolve_occurrence(ev, args.get("occurrence"))
    if occ is None:
        return tool_error(
            "This is a recurring event — pass 'occurrence' (the date of the occurrence, "
            "e.g. '2026-06-05') so the report attaches to the right day."
        )
    report = _build_report(args)
    if not report:
        return tool_error(
            "Provide at least one of: minutes, transcription, summary, outcome, notes, "
            "attendees, action_items, decisions, links, tags."
        )
    existing = store.get_report(event_id, occ)
    merged = {**existing["report"], **report} if (existing and isinstance(existing.get("report"), dict)) else report
    store.set_report(event_id, occ, merged)
    # Auto-confirm the occurrence when a report is saved, unless a live timer
    # is currently running (status='active') — don't clobber it.
    try:
        cur_status = store.get_status(event_id, occ)
        if cur_status is None or cur_status["status"] in ("floating", "missed", "confirmed"):
            store.set_status(event_id, occ, "confirmed", source="report")
    except Exception:
        pass
    return tool_result({"saved": True, "event_id": event_id, "title": ev.get("title"),
                        "occurrence_utc": occ, "report": merged})


def _handle_calendar_get_report(args: Dict[str, Any], **kw) -> str:
    event_id = _report_event_id(args)
    if not event_id:
        return tool_error("id (the event id) is required")
    ev = store.get_event(event_id)
    if ev is None:
        return tool_error(f"Event not found: {event_id}")
    occ = _resolve_occurrence(ev, args.get("occurrence"))
    if occ is None:
        return tool_error("Recurring event — pass 'occurrence' (the occurrence date).")
    rep = store.get_report(event_id, occ)
    if not rep:
        return tool_result({"found": False, "event_id": event_id,
                            "title": ev.get("title"), "occurrence_utc": occ})
    return tool_result({"found": True, "event_id": event_id, "title": ev.get("title"),
                        "occurrence_utc": occ, "report": rep["report"],
                        "created_utc": rep.get("created_utc"), "updated_utc": rep.get("updated_utc")})


def _handle_calendar_list_reports(args: Dict[str, Any], **kw) -> str:
    event_id = _report_event_id(args)
    if not event_id:
        return tool_error("id (the event id) is required")
    ev = store.get_event(event_id)
    if ev is None:
        return tool_error(f"Event not found: {event_id}")
    reps = store.list_reports(event_id)
    return tool_result({"event_id": event_id, "title": ev.get("title"), "count": len(reps),
                        "reports": [{"occurrence_utc": r["occurrence_utc"], "report": r["report"],
                                     "updated_utc": r.get("updated_utc")} for r in reps]})


_REPORT_FIELDS_DESC = (
    "Provide any of (they merge into an existing report): minutes, transcription, "
    "summary, outcome, notes (text); attendees, action_items, decisions, links, tags (lists)."
)

CALENDAR_SET_REPORT_SCHEMA = {
    "name": "calendar_set_report",
    "description": (
        "Attach/update a REPORT for a specific occurrence of an event — the minutes, "
        "transcription, attendees, decisions or outcome of a meeting/visio that happened. "
        "Works for one-time AND recurring events (for recurring, pass 'occurrence' = the date). "
        + _REPORT_FIELDS_DESC
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "The event id."},
            "occurrence": {"type": "string", "description": "Occurrence date/datetime — required for recurring events; omit for one-time."},
            "minutes": {"type": "string", "description": "Meeting minutes / what happened."},
            "transcription": {"type": "string", "description": "Full transcription of the meeting."},
            "summary": {"type": "string", "description": "Short summary."},
            "outcome": {"type": "string", "description": "Outcome / result."},
            "notes": {"type": "string", "description": "Freeform notes."},
            "attendees": {"type": "array", "items": {"type": "string"}, "description": "Who actually attended."},
            "action_items": {"type": "array", "items": {"type": "string"}, "description": "Action items / TODOs."},
            "decisions": {"type": "array", "items": {"type": "string"}, "description": "Decisions made."},
            "links": {"type": "array", "items": {"type": "string"}, "description": "Related links / recordings."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags."},
        },
        "required": ["id"],
    },
}

CALENDAR_GET_REPORT_SCHEMA = {
    "name": "calendar_get_report",
    "description": (
        "Read the report (minutes/transcription/attendees/outcome/…) for a specific "
        "occurrence of an event. For a recurring event pass 'occurrence' (the date)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "The event id."},
            "occurrence": {"type": "string", "description": "Occurrence date/datetime — required for recurring; omit for one-time."},
        },
        "required": ["id"],
    },
}

CALENDAR_LIST_REPORTS_SCHEMA = {
    "name": "calendar_list_reports",
    "description": (
        "List every report recorded for an event across its occurrences (e.g. all "
        "weekly-standup minutes). Returns each occurrence date and its report."
    ),
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "The event id."}},
        "required": ["id"],
    },
}


# ---------------------------------------------------------------------------
# Status lifecycle + work timers
# ---------------------------------------------------------------------------

def _handle_calendar_set_status(args: Dict[str, Any], **kw) -> str:
    event_id = str(args.get("id") or "").strip()
    if not event_id:
        return tool_error("id is required")
    ev = store.get_event(event_id)
    if ev is None:
        return tool_error(f"Event not found: {event_id}")

    occ = _resolve_occurrence(ev, args.get("occurrence"))
    if occ is None:
        return tool_error(
            "This is a recurring event — pass 'occurrence' (the date of the specific instance, "
            "e.g. '2026-06-10') so the status attaches to the right occurrence."
        )

    status = str(args.get("status") or "confirmed").strip().lower()
    if status not in _STATUS_ENUM:
        return tool_error(f"status must be one of: {_STATUS_ENUM}")

    note = args.get("note")

    if status == "floating":
        store.clear_status(event_id, occ)
    else:
        store.set_status(event_id, occ, status, note=note, source="manual")

    return tool_result({"id": event_id, "occurrence_utc": occ, "status": status})


def _handle_calendar_start_timer(args: Dict[str, Any], **kw) -> str:
    title = str(args.get("title") or "").strip()
    if not title:
        return tool_error("title is required")

    # Validate duration BEFORE creating the event so a bad value doesn't leave
    # an orphaned event behind, and the caller gets clear feedback.
    duration_raw = args.get("duration")
    duration_seconds: Optional[int] = None
    if duration_raw is not None:
        duration_seconds = _parse_lead(duration_raw)
        if duration_seconds is None:
            return tool_error(
                f"Couldn't understand duration {duration_raw!r} — use e.g. '2 hours', "
                "'90 min', '1h30m', or omit it for an open-ended timer."
            )

    tz_name = args.get("tz") or recurrence_mod.DEFAULT_TZ
    now = datetime.now(timezone.utc)

    tags_raw = args.get("tags")
    tags: Optional[List[str]] = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else None

    event_data = {
        "title": title,
        "description": args.get("description"),
        "start_utc": now.isoformat(),
        "tz": tz_name,
        "all_day": False,
        "recurrence": None,
        "alert_lead_seconds": None,
        "alert_channel": "none",
        "meeting": None,
        "location": args.get("location"),
        "tags": tags,
    }
    try:
        event_id = store.add_event(event_data)
    except Exception as e:
        logger.exception("calendar_start_timer store error")
        return tool_error(f"Failed to create timer event: {e}")

    occ_iso = now.isoformat()

    if duration_seconds is not None:
        ended_utc = (now + timedelta(seconds=duration_seconds)).isoformat()
        store.set_status(
            event_id, occ_iso, "confirmed",
            started_utc=now.isoformat(),
            ended_utc=ended_utc,
            duration_seconds=duration_seconds,
            source="timer",
        )
        status = "confirmed"
    else:
        store.set_status(
            event_id, occ_iso, "active",
            started_utc=now.isoformat(),
            source="timer",
        )
        status = "active"

    result: Dict[str, Any] = {
        "id": event_id,
        "title": title,
        "started_utc": now.isoformat(),
        "status": status,
    }
    if duration_seconds is not None:
        result["duration_seconds"] = duration_seconds
    return tool_result(result)


def _handle_calendar_stop_timer(args: Dict[str, Any], **kw) -> str:
    given_id = str(args.get("id") or "").strip() or None
    note = args.get("note")

    actives = store.list_active()

    if given_id:
        row = next((r for r in actives if r["event_id"] == given_id), None)
        if row is None:
            return tool_error(f"No running timer found for event: {given_id}")
    else:
        if len(actives) == 0:
            return tool_error("No running timer. Start one with calendar_start_timer.")
        if len(actives) > 1:
            lines = []
            for r in actives:
                ev = store.get_event(r["event_id"])
                title = ev["title"] if ev else r["event_id"]
                lines.append(f"  • {r['event_id']} — {title!r} (started {r.get('started_utc', '?')})")
            return tool_error(
                "Multiple active timers — pass 'id' to specify which to stop:\n" + "\n".join(lines)
            )
        row = actives[0]

    event_id = row["event_id"]
    occ_iso = row["occurrence_utc"]
    started_iso = row.get("started_utc")

    now = datetime.now(timezone.utc)
    measured: Optional[int] = None
    if started_iso:
        try:
            started_dt = datetime.fromisoformat(started_iso)
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=timezone.utc)
            measured = max(0, round((now - started_dt).total_seconds()))
        except Exception:
            pass

    store.set_status(
        event_id, occ_iso, "confirmed",
        ended_utc=now.isoformat(),
        duration_seconds=measured,
        note=note,
        source="timer",
    )

    ev = store.get_event(event_id)
    title = ev["title"] if ev else event_id

    result: Dict[str, Any] = {
        "id": event_id,
        "title": title,
        "started_utc": started_iso,
        "ended_utc": now.isoformat(),
    }
    if measured is not None:
        result["duration_seconds"] = measured
    return tool_result(result)


CALENDAR_SET_STATUS_SCHEMA = {
    "name": "calendar_set_status",
    "description": (
        "Record whether a calendar event (or a specific occurrence of a recurring event) "
        "actually happened — confirmed, missed, or reset to unknown. "
        "Use 'confirmed' when the meeting/task/event took place and you attended or did it. "
        "Use 'missed' when it did NOT happen (cancelled last-minute, skipped, no-show). "
        "Use 'floating' to reset an occurrence back to the default unknown state. "
        "For recurring events, pass 'occurrence' to target the right instance — the agent can "
        "resolve a natural-language reference like 'the Monday standup that happened at 9am' "
        "to the correct (id, occurrence) pair. "
        "An optional 'note' (reason, context) is stored alongside the status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Event ID."},
            "occurrence": {
                "type": "string",
                "description": (
                    "The datetime of the specific occurrence to update — required for recurring "
                    "events; for one-time events defaults to the event's own start time. "
                    "E.g. '2026-06-10T09:00:00+03:00' or simply '2026-06-10'."
                ),
            },
            "status": {
                "type": "string",
                "enum": _STATUS_ENUM,
                "description": (
                    "'confirmed' = it happened; 'missed' = it did not happen; "
                    "'floating' = reset to unknown (default for new occurrences)."
                ),
            },
            "note": {
                "type": "string",
                "description": "Optional free-text note stored alongside the status (reason, context, etc.).",
            },
        },
        "required": ["id"],
    },
}

CALENDAR_START_TIMER_SCHEMA = {
    "name": "calendar_start_timer",
    "description": (
        "Start a work timer right now — instantly creates a calendar event for this moment and "
        "begins tracking time. Ideal for unplanned tasks, spontaneous work sessions, or anything "
        "you want to log as you do it ('I'm starting the refactor now', 'begin the 1-on-1'). "
        "If you provide a 'duration' (e.g. '2 hours', '90 min') the block is fully fixed and "
        "marked confirmed immediately. If you omit duration the timer runs open-ended — stop it "
        "later with calendar_stop_timer to record the measured elapsed time. "
        "No reminder is set (alert_channel=none) since you're already doing the task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What you are starting (required)."},
            "duration": {
                "type": "string",
                "description": (
                    "Optional fixed duration, e.g. '2 hours', '90 min', '45 minutes', '1 hour 30 min'. "
                    "If omitted the timer is open-ended and must be stopped with calendar_stop_timer."
                ),
            },
            "description": {"type": "string", "description": "Optional notes about what is being worked on."},
            "location": {"type": "string", "description": "Optional location."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for filtering, e.g. ['work', 'deep-focus'].",
            },
            "tz": {
                "type": "string",
                "description": (
                    f"IANA timezone for the event, e.g. 'Indian/Antananarivo'. "
                    f"Defaults to {recurrence_mod.DEFAULT_TZ}."
                ),
            },
        },
        "required": ["title"],
    },
}

CALENDAR_STOP_TIMER_SCHEMA = {
    "name": "calendar_stop_timer",
    "description": (
        "Stop a running work timer and record the measured elapsed duration. "
        "If there is exactly one active timer, it is stopped automatically — no id needed. "
        "If there are multiple active timers, pass 'id' to specify which one to stop "
        "(the tool will list the choices if you omit id and multiple are running). "
        "The timer's occurrence is marked 'confirmed' and the duration in seconds is stored. "
        "An optional 'note' (what was accomplished, blockers, etc.) is saved alongside."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": (
                    "Event ID of the timer to stop. Omit if there is only one running timer "
                    "(the single active one is stopped automatically)."
                ),
            },
            "note": {
                "type": "string",
                "description": "Optional note about what was accomplished or why the timer is stopping.",
            },
        },
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
    ("calendar_set_report",   CALENDAR_SET_REPORT_SCHEMA,   _handle_calendar_set_report,   "📝"),
    ("calendar_get_report",   CALENDAR_GET_REPORT_SCHEMA,   _handle_calendar_get_report,   "📖"),
    ("calendar_list_reports", CALENDAR_LIST_REPORTS_SCHEMA, _handle_calendar_list_reports, "🗂️"),
    ("calendar_set_status",   CALENDAR_SET_STATUS_SCHEMA,   _handle_calendar_set_status,   "✅"),
    ("calendar_start_timer",  CALENDAR_START_TIMER_SCHEMA,  _handle_calendar_start_timer,  "⏱️"),
    ("calendar_stop_timer",   CALENDAR_STOP_TIMER_SCHEMA,   _handle_calendar_stop_timer,   "⏹️"),
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

    # Alert delivery is driven SOLELY by the every-minute
    # `hermes cron --no-agent --script calendar_tick.py` job (see README): it
    # fires reminders regardless of agent activity and is the only path that can
    # deliver the "chat" channel (its stdout is posted into the chat). We do NOT
    # start the in-gateway scheduler thread here — a second firing path would
    # race the cron on the per-occurrence fired_alerts dedup and could mark a
    # chat reminder fired before the cron gets a chance to print it.
