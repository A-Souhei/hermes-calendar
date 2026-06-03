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

from . import digest as digest_mod
from . import job_report as job_report_mod
from . import notify
from . import planning as planning_mod
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

_CHANNEL_ENUM = ["ha_notify", "ha_speak", "both", "chat", "email", "all", "none"]
_VALID_CHANNELS = set(_CHANNEL_ENUM)
_CHANNEL_DESCRIPTION = (
    "Delivery channel for reminders (default: ha_notify). "
    "'ha_notify' = phone push; 'ha_speak' = spoken TTS on the phone; "
    "'both' = push + speak; 'chat' = a chat message in this conversation; "
    "'email' = emailed reminder to the asker's registered address (set via "
    "calendar_set_user_email) or an explicit notify_email; only sends to "
    "addresses in the allowlist, and sends nothing if no address is known; "
    "'all' = push + speak + chat + email; 'none' = no reminder."
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_OWNER_DESCRIPTION = (
    "REQUIRED — the person this event belongs to (the asker). Every event must "
    "have an owner so it appears on that person's calendar and nobody else's; "
    "unassigned events are not allowed. Set it from who is making the request, "
    "and use a CONSISTENT identifier per person (the same value each time) so "
    "their events group together and their email association resolves."
)

_NOTIFY_EMAIL_DESCRIPTION = (
    "Explicit email address to send this event's email reminder to (overrides "
    "the owner's registered address). Only used if the address is in the "
    "allowlist; otherwise no email is sent."
)

_LANGUAGE_ENUM = ["en", "fr"]
_LANGUAGE_DESCRIPTION = (
    "Language for THIS event's reminder text — the labels and the date are rendered "
    "in this language ('en' = English, 'fr' = French). "
    "Infer it from how the user phrased the request: a French-phrased request → 'fr', "
    "an English one → 'en'. "
    "Omit to use the configured default (CALENDAR_DEFAULT_LANG env or 'en')."
)


def _until_local_date(until_iso: str, tz_name: Optional[str]) -> str:
    """The recurrence `until` (stored UTC) as a date string in the event's
    local timezone — so the badge shows the date the user actually meant,
    not the UTC date."""
    try:
        from zoneinfo import ZoneInfo
        s = str(until_iso).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:          # date-only / no offset -> treat as UTC
            dt = dt.replace(tzinfo=timezone.utc)
        if tz_name:
            dt = dt.astimezone(ZoneInfo(tz_name))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return str(until_iso)[:10]


def _human_recurrence(rec: Optional[Dict], tz_name: Optional[str] = None) -> Optional[str]:
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
        label += f" until {_until_local_date(rec['until'], tz_name)}"
    if rec.get("count"):
        label += f" ({rec['count']} times)"
    return label


def _resolve_planning(id_or_name: Any, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve a planning by id first, then case-insensitive name.

    When owner is provided the name lookup is STRICTLY scoped to that owner
    (no cross-owner fallback), so two users can have plannings with the same
    name without one resolving the other's. Cross-user lookups still work
    because the id lookup is tried first.
    """
    key = str(id_or_name or "").strip()
    if not key:
        return None
    p = store.get_planning(key)
    if p is None:
        p = store.get_planning_by_name(key, owner=owner)
    return p


def _planning_name_for(ev: Dict) -> Optional[str]:
    """Return the planning name for an event with planning_id set, else None."""
    pid = ev.get("planning_id")
    if not pid:
        return None
    try:
        p = store.get_planning(pid)
        return p["name"] if p else None
    except Exception:
        return None


def _event_summary(ev: Dict) -> Dict:
    """Return a compact summary suitable for tool responses."""
    return {
        "id": ev["id"],
        "title": ev["title"],
        "start_utc": ev["start_utc"],
        "tz": ev.get("tz"),
        "all_day": ev.get("all_day", False),
        "recurrence": _human_recurrence(ev.get("recurrence"), ev.get("tz")),
        "alert_lead_seconds": ev.get("alert_lead_seconds"),
        "alert_channel": ev.get("alert_channel"),
        "language": ev.get("language"),
        "location": ev.get("location"),
        "tags": ev.get("tags"),
        "owner": ev.get("owner"),
        "notify_email": ev.get("notify_email"),
        "planning": _planning_name_for(ev),
        "job": ev.get("job"),
        "category": ev.get("category"),
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

    lang_raw = args.get("language")
    language: Optional[str] = None
    if lang_raw is not None:
        lang_lower = str(lang_raw).strip().lower()
        language = lang_lower if lang_lower in _LANGUAGE_ENUM else None

    owner_raw = args.get("owner")
    owner = (str(owner_raw).strip() or None) if owner_raw is not None else None
    notify_email_raw = args.get("notify_email")
    notify_email = (
        (str(notify_email_raw).strip().lower() or None)
        if notify_email_raw is not None else None
    )

    # Optional attachment to a planning (by id or name). When attached:
    #   - tag the event with planning_id;
    #   - bound a recurring series to the planning's period end (unless the
    #     caller gave an explicit `until`);
    #   - inherit the planning's owner/language when not supplied.
    planning_id: Optional[str] = None
    planning_raw = args.get("planning")
    if planning_raw is not None and str(planning_raw).strip():
        planning = _resolve_planning(planning_raw, owner=owner)
        if planning is None:
            return tool_error(f"Planning not found: {planning_raw!r}")
        planning_id = planning["id"]
        if rec is not None and not rec.get("until"):
            rec = dict(rec)
            rec["until"] = planning["period_end_utc"]
        if owner is None and planning.get("owner"):
            owner = planning["owner"]
        if language is None and planning.get("language") in _LANGUAGE_ENUM:
            language = planning["language"]

    # Every event must belong to a user — unassigned events are not allowed.
    if not owner:
        return tool_error(
            "owner is required — every event must belong to a user. Set 'owner' "
            "to the person this event is for (typically the asker)."
        )

    category_raw = args.get("category")
    category = (str(category_raw).strip() or None) if category_raw is not None else None

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
        "language": language,
        "owner": owner,
        "notify_email": notify_email,
        "planning_id": planning_id,
        "category": category,
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

    if "language" in args:
        lang_raw = args["language"]
        if lang_raw is None:
            fields["language"] = None
        else:
            lang_lower = str(lang_raw).strip().lower()
            fields["language"] = lang_lower if lang_lower in _LANGUAGE_ENUM else None

    if "owner" in args:
        owner_raw = args["owner"]
        new_owner = str(owner_raw).strip() if owner_raw is not None else ""
        if not new_owner:
            return tool_error(
                "owner cannot be cleared — every event must belong to a user. "
                "Pass a valid owner, or omit 'owner' to leave it unchanged."
            )
        fields["owner"] = new_owner

    if "notify_email" in args:
        ne_raw = args["notify_email"]
        fields["notify_email"] = (
            (str(ne_raw).strip().lower() or None) if ne_raw is not None else None
        )

    if "category" in args:
        cat_raw = args["category"]
        fields["category"] = (str(cat_raw).strip() or None) if cat_raw is not None else None

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
    owner_filter = (str(args["owner"]).strip() or None) if args.get("owner") else None

    try:
        events = store.list_events(owner=owner_filter)
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
        planning_name = _planning_name_for(ev)
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
                "planning": planning_name,
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
        "recurrence_human": _human_recurrence(ev.get("recurrence"), ev.get("tz")),
        "alert_lead_seconds": ev.get("alert_lead_seconds"),
        "alert_channel": ev.get("alert_channel"),
        "language": ev.get("language"),
        "owner": ev.get("owner"),
        "notify_email": ev.get("notify_email"),
        "planning": _planning_name_for(ev),
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
# User email registry handlers
# ---------------------------------------------------------------------------

def _handle_calendar_set_user_email(args: Dict[str, Any], **kw) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return tool_error("name is required")
    email = str(args.get("email") or "").strip().lower()
    if not email or not _EMAIL_RE.match(email):
        return tool_error(f"That does not look like a valid email address: {args.get('email')!r}")

    allowed = notify.allowed_email_recipients()
    if email not in allowed:
        return tool_error(
            f"Refusing to store {email!r}: email reminders may only be sent to "
            "allowlisted addresses (EMAIL_ALLOWED_USERS). Ask an admin to add it "
            "to the allowlist before associating it."
        )

    try:
        store.set_user_email(name, email)
    except Exception as e:
        logger.exception("calendar_set_user_email store error")
        return tool_error(f"Failed to store email association: {e}")
    return tool_result({"associated": name, "email": email})


def _handle_calendar_list_user_emails(args: Dict[str, Any], **kw) -> str:
    try:
        return tool_result({"associations": store.list_user_emails()})
    except Exception as e:
        return tool_error(f"Failed to list email associations: {e}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_PLANNING_PARAM_DESCRIPTION = (
    "Optional: attach this event to an existing PLANNING (pass the planning's "
    "name or id). A planning is a named, period-bounded set of events; attaching "
    "tags the event so it is scored in the planning's report. If the event is "
    "RECURRING and you don't pass an explicit `until`, its series is automatically "
    "bounded to the planning's period end (occurrences stop at period end). The "
    "event also inherits the planning's owner and language when you don't supply "
    "them. Create the planning first with calendar_create_planning."
)

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
            "language": {
                "type": ["string", "null"],
                "enum": ["en", "fr", None],
                "description": _LANGUAGE_DESCRIPTION,
            },
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "notify_email": {"type": ["string", "null"], "description": _NOTIFY_EMAIL_DESCRIPTION},
            "planning": {"type": ["string", "null"], "description": _PLANNING_PARAM_DESCRIPTION},
            "category": {
                "type": "string",
                "description": (
                    "Optional free-text category to group this event for reports, "
                    "e.g. 'work', 'personal', 'client-acme'."
                ),
            },
        },
        "required": ["title", "start", "owner"],
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
            "language": {
                "type": ["string", "null"],
                "enum": ["en", "fr", None],
                "description": _LANGUAGE_DESCRIPTION,
            },
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "notify_email": {"type": ["string", "null"], "description": _NOTIFY_EMAIL_DESCRIPTION},
            "category": {
                "type": "string",
                "description": (
                    "Optional free-text category to group this event for reports, "
                    "e.g. 'work', 'personal', 'client-acme'."
                ),
            },
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
        "Defaults to now … now+30 days. "
        "ALWAYS pass 'owner' to scope results to the person asking — omit only when "
        "an admin explicitly wants to see every user's events at once."
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
            "owner": {
                "type": "string",
                "description": (
                    "Filter events by owner. Pass the identifier of the person asking "
                    "(same value used as 'owner' when events were created) to show only "
                    "their calendar. Omit to return all users' events."
                ),
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


def _start_timer_impl(args: Dict[str, Any]) -> tuple:
    """Core of calendar_start_timer. Returns (ok, payload): (True, result_dict)
    on success or (False, error_message) on failure. Shared by
    calendar_start_timer and calendar_resume_job."""
    title = str(args.get("title") or "").strip()
    if not title:
        return (False, "title is required")

    # Validate duration BEFORE creating the event so a bad value doesn't leave
    # an orphaned event behind, and the caller gets clear feedback.
    duration_raw = args.get("duration")
    duration_seconds: Optional[int] = None
    if duration_raw is not None:
        duration_seconds = _parse_lead(duration_raw)
        if duration_seconds is None:
            return (False,
                f"Couldn't understand duration {duration_raw!r} — use e.g. '2 hours', "
                "'90 min', '1h30m', or omit it for an open-ended timer."
            )

    tz_name = args.get("tz") or recurrence_mod.DEFAULT_TZ
    now = datetime.now(timezone.utc)

    tags_raw = args.get("tags")
    tags: Optional[List[str]] = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else None

    lang_raw = args.get("language")
    timer_language: Optional[str] = None
    if lang_raw is not None:
        lang_lower = str(lang_raw).strip().lower()
        timer_language = lang_lower if lang_lower in _LANGUAGE_ENUM else None

    owner_raw = args.get("owner")
    timer_owner = (str(owner_raw).strip() or None) if owner_raw is not None else None
    if not timer_owner:
        return (False,
            "owner is required — every event (including timers) must belong to a "
            "user. Set 'owner' to the person this timer is for (typically the asker)."
        )
    notify_email_raw = args.get("notify_email")
    timer_notify_email = (
        (str(notify_email_raw).strip().lower() or None)
        if notify_email_raw is not None else None
    )

    job_raw = args.get("job")
    timer_job = (str(job_raw).strip() or None) if job_raw is not None else None

    category_raw = args.get("category")
    timer_category = (str(category_raw).strip() or None) if category_raw is not None else None

    # Auto-switch: stop any running timers for THIS owner before starting the new one.
    existing_actives = store.list_active(owner=timer_owner)
    switched_from: List[Dict[str, Any]] = []
    for active_row in existing_actives:
        stopped = _stop_active_row(active_row)
        switched_from.append({
            "id": stopped["id"],
            "title": stopped["title"],
            "duration_seconds": stopped["duration_seconds"],
        })

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
        "language": timer_language,
        "owner": timer_owner,
        "notify_email": timer_notify_email,
        "job": timer_job,
        "category": timer_category,
    }
    try:
        event_id = store.add_event(event_data)
    except Exception as e:
        logger.exception("calendar_start_timer store error")
        return (False, f"Failed to create timer event: {e}")

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
    if switched_from:
        result["switched_from"] = switched_from
        # Build human warning: list stopped jobs/titles
        def _fmt_switched(s: Dict) -> str:
            d = s.get("duration_seconds")
            t = s.get("title") or s.get("id")
            if d is not None:
                m = max(0, round(d / 60))
                return f"'{t}' ({m}m)"
            return f"'{t}'"
        stopped_labels = ", ".join(_fmt_switched(s) for s in switched_from)
        result["warning"] = f"Stopped running job {stopped_labels} and started '{title}'."
    return (True, result)


def _handle_calendar_start_timer(args: Dict[str, Any], **kw) -> str:
    ok, payload = _start_timer_impl(args)
    return tool_result(payload) if ok else tool_error(payload)


def _handle_calendar_resume_job(args: Dict[str, Any], **kw) -> str:
    owner_raw = args.get("owner")
    owner = (str(owner_raw).strip() or None) if owner_raw is not None else None
    if not owner:
        return tool_error("owner is required — the user this job belongs to.")
    job_raw = args.get("job")
    job = (str(job_raw).strip() or None) if job_raw is not None else None
    if not job:
        return tool_error("job is required — the name of the job to resume.")

    match = store.find_job_event(owner, job)
    if match is None:
        # No match: ask for the exact name and surface the existing jobs so the
        # agent (or user) can pick the right spelling rather than silently
        # creating a near-duplicate job that won't aggregate.
        try:
            existing = store.list_jobs(owner)
        except Exception:
            existing = []
        names = sorted({j["job"] for j in existing if j.get("job")})
        if names:
            listed = ", ".join(repr(n) for n in names)
            return tool_error(
                f"No job named {job!r} found for {owner}. Existing jobs: {listed}. "
                "Ask the user which one to resume, then call again with the exact name."
            )
        return tool_error(
            f"No tracked job named {job!r} for {owner}, and this user has no jobs yet. "
            "Use calendar_start_timer to begin a brand-new job."
        )

    # Reuse the EXACT stored job spelling + its category/title so the resumed
    # session aggregates with the prior ones. Explicit args still override.
    start_args: Dict[str, Any] = {
        "owner": owner,
        "job": match.get("job") or job,
        "title": args.get("title") or match.get("title") or match.get("job"),
        "category": args.get("category") if args.get("category") is not None else match.get("category"),
    }
    if args.get("duration") is not None:
        start_args["duration"] = args["duration"]
    if args.get("description") is not None:
        start_args["description"] = args["description"]

    ok, payload = _start_timer_impl(start_args)
    if not ok:
        return tool_error(payload)
    payload["resumed"] = True
    payload["resumed_from"] = {
        "job": match.get("job"),
        "category": match.get("category"),
        "last_session_utc": match.get("start_utc"),
    }
    return tool_result(payload)


def _stop_active_row(row: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
    """Stop one active timer row: confirm it, record measured duration.

    Returns a dict with id, title, started_utc, ended_utc, duration_seconds.
    """
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
    return {
        "id": event_id,
        "title": title,
        "started_utc": started_iso,
        "ended_utc": now.isoformat(),
        "duration_seconds": measured,
    }


def _handle_calendar_stop_timer(args: Dict[str, Any], **kw) -> str:
    given_id = str(args.get("id") or "").strip() or None
    note = args.get("note")
    owner_raw = args.get("owner")
    scope_owner = (str(owner_raw).strip() or None) if owner_raw is not None else None

    actives = store.list_active(owner=scope_owner)

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

    stopped = _stop_active_row(row, note=note)
    result: Dict[str, Any] = {
        "id": stopped["id"],
        "title": stopped["title"],
        "started_utc": stopped["started_utc"],
        "ended_utc": stopped["ended_utc"],
    }
    if stopped["duration_seconds"] is not None:
        result["duration_seconds"] = stopped["duration_seconds"]
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
        "No reminder is set (alert_channel=none) since you're already doing the task. "
        "IMPORTANT: only ONE timer can run at a time per user. Starting a new timer when one is "
        "already running AUTO-STOPS the running one (records its measured duration) and starts the "
        "new one. The result will include a 'warning' and 'switched_from' list when this happens. "
        "Use 'job' to tag this timer to a named work-stream for time-tracking reports."
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
            "job": {
                "type": "string",
                "description": (
                    "The job/work-stream this timer logs time against — used for time-tracking reports, "
                    "e.g. 'client-acme', 'thesis-writing'. Free text; reuse the same string to "
                    "accumulate time across sessions."
                ),
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional free-text category to group this work for reports, "
                    "e.g. 'work', 'personal', 'client-acme'."
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
            "language": {
                "type": ["string", "null"],
                "enum": ["en", "fr", None],
                "description": _LANGUAGE_DESCRIPTION,
            },
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "notify_email": {"type": ["string", "null"], "description": _NOTIFY_EMAIL_DESCRIPTION},
        },
        "required": ["title", "owner"],
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
        "An optional 'note' (what was accomplished, blockers, etc.) is saved alongside. "
        "Pass 'owner' to scope the active-timer lookup to a specific user."
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
            "owner": {
                "type": "string",
                "description": (
                    _OWNER_DESCRIPTION
                    + " When provided, only this user's active timers are considered as the "
                    "'current running timer'. Omit to consider all users' timers globally."
                ),
            },
        },
    },
}

CALENDAR_RESUME_JOB_SCHEMA = {
    "name": "calendar_resume_job",
    "description": (
        "Resume tracking time on an EXISTING job — starts a fresh timer session that reuses the "
        "job's exact stored name and category, so the new session aggregates with the previous "
        "ones in reports (rather than creating a near-duplicate job). Use this whenever the user "
        "wants to continue/resume a job they worked on before ('resume client-acme', 'start the "
        "thesis work again', 'continue yesterday's refactor'). Like calendar_start_timer this "
        "auto-stops the user's currently running timer (the result reports it via 'warning'/"
        "'switched_from'). If the given job name does not match an existing job for the user, the "
        "tool returns the list of existing jobs and asks you to confirm the exact name with the "
        "user — do NOT invent a new job here; use calendar_start_timer for genuinely new jobs. "
        "Provide an optional 'duration' for a fixed block, otherwise the session is open-ended "
        "and stopped later with calendar_stop_timer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "job": {
                "type": "string",
                "description": (
                    "The name of the job to resume. Matched case-insensitively against the "
                    "user's existing jobs; the exact stored spelling is reused so sessions "
                    "aggregate. If unsure of the exact name, call calendar_list_jobs first."
                ),
            },
            "duration": {
                "type": "string",
                "description": (
                    "Optional fixed duration, e.g. '2 hours', '90 min'. Omit for an open-ended "
                    "session stopped later with calendar_stop_timer."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional title for this session. Defaults to the prior session's title.",
            },
            "category": {
                "type": "string",
                "description": "Optional category override. Defaults to the job's existing category.",
            },
            "description": {"type": "string", "description": "Optional notes about this session."},
        },
        "required": ["owner", "job"],
    },
}


CALENDAR_SET_USER_EMAIL_SCHEMA = {
    "name": "calendar_set_user_email",
    "description": (
        "Associate a person's name/identifier with their email address so EMAIL-channel "
        "reminders reach them. When someone first asks for email reminders, ASK them for "
        "the address they want reminders sent to, then call this with their consistent "
        "identifier (the same 'owner' you set on their events) and that address. "
        "SECURITY: only addresses in the configured allowlist (EMAIL_ALLOWED_USERS) are "
        "accepted — a non-allowlisted address is rejected and not stored. Once associated, "
        "any event whose owner matches this name and whose channel includes 'email' will be "
        "emailed here (unless the event overrides it with an explicit notify_email)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "The person's name/identifier — use the SAME value you set as an event's "
                    "'owner' so reminders resolve. Case-insensitive."
                ),
            },
            "email": {
                "type": "string",
                "description": "Their email address. Must be one of the allowlisted addresses.",
            },
        },
        "required": ["name", "email"],
    },
}

CALENDAR_LIST_USER_EMAILS_SCHEMA = {
    "name": "calendar_list_user_emails",
    "description": (
        "List all name -> email associations the calendar knows about (so you or the user "
        "can see who email reminders are routed to). Takes no arguments."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


# ---------------------------------------------------------------------------
# Plannings — named, period-bounded sets of events with emailed reports
# ---------------------------------------------------------------------------

def _planning_summary(p: Dict[str, Any]) -> Dict[str, Any]:
    """Compact planning summary for tool responses."""
    return {
        "id": p["id"],
        "name": p["name"],
        "period_label": p.get("period_label"),
        "period_start_utc": p.get("period_start_utc"),
        "period_end_utc": p.get("period_end_utc"),
        "owner": p.get("owner"),
        "language": p.get("language"),
        "description": p.get("description"),
        "report_sent": bool(p.get("report_sent")),
        "report_sent_utc": p.get("report_sent_utc"),
    }


def _handle_calendar_create_planning(args: Dict[str, Any], **kw) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return tool_error("name is required")

    start_raw = args.get("period_start")
    end_raw = args.get("period_end")
    if not start_raw or not end_raw:
        return tool_error(
            "period_start and period_end are required — period_end is EXCLUSIVE "
            "(the start of the day AFTER the period). E.g. June 2026 → "
            "period_start '2026-06-01', period_end '2026-07-01'."
        )

    tz_name = args.get("tz") or recurrence_mod.DEFAULT_TZ
    start_dt = _parse_start(start_raw, tz_name)
    if start_dt is None:
        return tool_error(f"Could not parse period_start: {start_raw!r}")
    end_dt = _parse_start(end_raw, tz_name)
    if end_dt is None:
        return tool_error(f"Could not parse period_end: {end_raw!r}")

    start_utc = start_dt.astimezone(timezone.utc).isoformat()
    end_utc = end_dt.astimezone(timezone.utc).isoformat()
    if end_dt <= start_dt:
        return tool_error("period_end must be after period_start (and is exclusive).")

    owner_raw = args.get("owner")
    owner = (str(owner_raw).strip() or None) if owner_raw is not None else None
    if not owner or store.get_user_email(owner) is None:
        return tool_error(
            f"Planning needs an email for {owner!r}. Associate one first with "
            "calendar_set_user_email (reports are emailed)."
        )

    lang_raw = args.get("language")
    language: Optional[str] = None
    if lang_raw is not None:
        lang_lower = str(lang_raw).strip().lower()
        language = lang_lower if lang_lower in _LANGUAGE_ENUM else None

    d = {
        "name": name,
        "period_label": args.get("period_label"),
        "period_start_utc": start_utc,
        "period_end_utc": end_utc,
        "owner": owner,
        "language": language,
        "tz": tz_name,
        "description": args.get("description"),
    }
    try:
        planning_id = store.add_planning(d)
    except Exception as e:
        logger.exception("calendar_create_planning store error")
        return tool_error(f"Failed to create planning: {e}")

    p = store.get_planning(planning_id)
    return tool_result({"created": True, **_planning_summary(p)})


def _handle_calendar_list_plannings(args: Dict[str, Any], **kw) -> str:
    owner_filter = (str(args["owner"]).strip() or None) if args.get("owner") else None
    try:
        plannings = store.list_plannings(owner=owner_filter)
    except Exception as e:
        return tool_error(f"Failed to list plannings: {e}")
    out = []
    for p in plannings:
        summary = _planning_summary(p)
        try:
            stats = planning_mod.planning_stats(p)["overall"]
            summary["confirmed"] = stats["confirmed"]
            summary["total"] = stats["total"]
            summary["completion_pct"] = stats["completion_pct"]
        except Exception:
            summary["confirmed"] = summary["total"] = summary["completion_pct"] = 0
        out.append(summary)
    return tool_result({"count": len(out), "plannings": out})


def _handle_calendar_get_planning(args: Dict[str, Any], **kw) -> str:
    owner_ctx = (str(args["owner"]).strip() or None) if args.get("owner") else None
    p = _resolve_planning(args.get("id_or_name"), owner=owner_ctx)
    if p is None:
        return tool_error(f"Planning not found: {args.get('id_or_name')!r}")
    try:
        stats = planning_mod.planning_stats(p)
    except Exception as e:
        logger.exception("calendar_get_planning stats error")
        return tool_error(f"Failed to compute planning stats: {e}")

    events = []
    for ev in store.list_planning_events(p["id"]):
        events.append({
            "id": ev["id"],
            "title": ev["title"],
            "start_utc": ev.get("start_utc"),
            "recurrence_human": _human_recurrence(ev.get("recurrence"), ev.get("tz")),
        })

    return tool_result({
        **_planning_summary(p),
        "events": events,
        "overall": stats["overall"],
        "objectives": stats["objectives"],
        "report_text": stats["text"],
    })


def _planning_report_filename(planning: Dict[str, Any]) -> str:
    """Localized-ish PDF filename: planning name with non-alnum collapsed to '-'."""
    import re
    name = planning.get("name") or "planning"
    safe = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "planning"
    return f"planning-report-{safe}.pdf"


def _handle_calendar_planning_report(args: Dict[str, Any], **kw) -> str:
    owner_ctx = (str(args["owner"]).strip() or None) if args.get("owner") else None
    p = _resolve_planning(args.get("id_or_name"), owner=owner_ctx)
    if p is None:
        return tool_error(f"Planning not found: {args.get('id_or_name')!r}")

    try:
        stats = planning_mod.planning_stats(p)
    except Exception as e:
        logger.exception("calendar_planning_report stats error")
        return tool_error(f"Failed to compute planning report: {e}")

    do_email = bool(args.get("email", False))
    emailed = False
    pdf_attached = False
    email_error: Optional[str] = None
    if do_email:
        owner_email = store.get_user_email(p["owner"]) if p.get("owner") else None
        if not owner_email:
            email_error = "no registered email for the planning owner"
        else:
            allowed = notify.allowed_email_recipients()
            if owner_email.lower() not in allowed:
                email_error = "owner email not allowlisted"
            else:
                lang = planning_mod._planning_lang(p)
                pdf = planning_mod.render_report_pdf(stats, lang)
                attachments = None
                if pdf:
                    fname = _planning_report_filename(p)
                    attachments = [(fname, pdf, "pdf")]
                result = notify.fire(
                    "email",
                    planning_mod.report_subject(p),
                    stats["text"],
                    target=owner_email,
                    attachments=attachments,
                )
                if result.get("ok"):
                    emailed = True
                    pdf_attached = bool(pdf)
                else:
                    email_error = result.get("error") or "email send failed"

    return tool_result({
        **_planning_summary(p),
        "overall": stats["overall"],
        "objectives": stats["objectives"],
        "report_text": stats["text"],
        "emailed": emailed,
        "pdf_attached": pdf_attached,
        "email_error": email_error,
    })


def _handle_calendar_remove_planning(args: Dict[str, Any], **kw) -> str:
    owner_ctx = (str(args["owner"]).strip() or None) if args.get("owner") else None
    p = _resolve_planning(args.get("id_or_name"), owner=owner_ctx)
    if p is None:
        return tool_error(f"Planning not found: {args.get('id_or_name')!r}")
    remove_events = bool(args.get("remove_events", False))
    try:
        removed = store.remove_planning(p["id"], remove_events=remove_events)
    except Exception as e:
        logger.exception("calendar_remove_planning store error")
        return tool_error(f"Failed to remove planning: {e}")
    if not removed:
        return tool_error(f"Planning not found: {p['id']}")
    return tool_result({
        "removed": True, "id": p["id"], "name": p["name"],
        "removed_events": remove_events,
    })


CALENDAR_CREATE_PLANNING_SCHEMA = {
    "name": "calendar_create_planning",
    "description": (
        "Create a PLANNING — a named set of events bound to a period (a week, "
        "month, year, or custom range). A planning groups time-bound objectives "
        "and is scored from each event's per-occurrence status: only CONFIRMED "
        "occurrences count as completed; everything else (unconfirmed, missed) "
        "counts as not done. Reports are EMAILED to the owner (the chat only "
        "announces a report is ready) — so the owner MUST have a registered email "
        "(set via calendar_set_user_email) or creation is refused. A report is "
        "auto-emailed once at 09:00 the morning after the period ends, and can "
        "also be produced on demand with calendar_planning_report. "
        "COMPUTE period_start and period_end yourself from the user's phrasing: "
        "period_end is EXCLUSIVE — pass the START of the day AFTER the period. "
        "E.g. 'June 2026' → period_start '2026-06-01', period_end '2026-07-01'; "
        "'2026' → '2026-01-01'..'2027-01-01'; 'first week of June 2026' → "
        "'2026-06-01'..'2026-06-08'. After creating, attach events to it via the "
        "`planning` param of calendar_add_event. If a desired objective lacks a "
        "time or period, ASK the user for precisions before creating events."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Planning name (required), e.g. 'June objectives'."},
            "period_start": {
                "type": "string",
                "description": "Start of the period (datetime/date string), e.g. '2026-06-01'.",
            },
            "period_end": {
                "type": "string",
                "description": (
                    "EXCLUSIVE end of the period — the START of the day AFTER the "
                    "last day. E.g. for June 2026 pass '2026-07-01'; for the year "
                    "2026 pass '2027-01-01'."
                ),
            },
            "period_label": {
                "type": "string",
                "description": "Optional human display label for the period, e.g. 'June 2026'.",
            },
            "owner": {
                "type": "string",
                "description": (
                    "Name/identifier of the asker who owns this planning — MUST "
                    "have a registered email (the report is emailed to them). Use "
                    "the same consistent identifier as their event 'owner'."
                ),
            },
            "language": {
                "type": ["string", "null"],
                "enum": ["en", "fr", None],
                "description": _LANGUAGE_DESCRIPTION,
            },
            "description": {"type": "string", "description": "Optional free-text description of the planning."},
            "tz": {
                "type": "string",
                "description": (
                    f"IANA timezone used to interpret period_start/period_end. "
                    f"Defaults to {recurrence_mod.DEFAULT_TZ}."
                ),
            },
        },
        "required": ["name", "period_start", "period_end", "owner"],
    },
}

CALENDAR_LIST_PLANNINGS_SCHEMA = {
    "name": "calendar_list_plannings",
    "description": (
        "List plannings with a quick completion snapshot for each "
        "(confirmed/total occurrences and completion percentage). "
        "ALWAYS pass 'owner' to scope results to the person asking — omit only when "
        "an admin explicitly wants to see all users' plannings at once."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {
                "type": "string",
                "description": (
                    "Filter plannings by owner. Pass the identifier of the person asking "
                    "to show only their plannings. Omit to return all users' plannings."
                ),
            },
        },
    },
}

CALENDAR_GET_PLANNING_SCHEMA = {
    "name": "calendar_get_planning",
    "description": (
        "Get a planning's details, its events (titles + recurrence), and computed "
        "completion stats per objective and overall (confirmed = done, everything "
        "else = not done), plus the rendered report text."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id_or_name": {"type": "string", "description": "Planning id or name (case-insensitive)."},
            "owner": {"type": "string", "description": "Owner identifier — used to disambiguate when two users have a planning with the same name."},
        },
        "required": ["id_or_name"],
    },
}

CALENDAR_PLANNING_REPORT_SCHEMA = {
    "name": "calendar_planning_report",
    "description": (
        "Produce a planning's completion report on demand. Returns the structured "
        "stats and the rendered report text. Reports are EMAIL-ONLY: set email=true "
        "to email it to the planning owner's registered address (only sent if that "
        "address is allowlisted). In chat, just announce that the report is ready — "
        "the detail goes by email."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id_or_name": {"type": "string", "description": "Planning id or name (case-insensitive)."},
            "email": {
                "type": "boolean",
                "description": "If true, email the report to the planning owner. Default false.",
            },
            "owner": {"type": "string", "description": "Owner identifier — used to disambiguate when two users have a planning with the same name."},
        },
        "required": ["id_or_name"],
    },
}

CALENDAR_REMOVE_PLANNING_SCHEMA = {
    "name": "calendar_remove_planning",
    "description": (
        "Remove a planning. By default the planning's events are KEPT (just "
        "detached — their planning_id is cleared). Set remove_events=true to also "
        "delete every event in the planning (cascading their statuses, reports and "
        "skipped-occurrence exceptions)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id_or_name": {"type": "string", "description": "Planning id or name (case-insensitive)."},
            "remove_events": {
                "type": "boolean",
                "description": "If true, also delete the planning's events. Default false (detach only).",
            },
            "owner": {"type": "string", "description": "Owner identifier — used to disambiguate when two users have a planning with the same name."},
        },
        "required": ["id_or_name"],
    },
}


# ---------------------------------------------------------------------------
# Calendar digest (on-demand)
# ---------------------------------------------------------------------------

def _handle_calendar_digest(args: Dict[str, Any], **kw) -> str:
    owner = str(args.get("owner") or "").strip()
    if not owner:
        return tool_error("owner is required")

    try:
        d = digest_mod.build_owner_digest(owner)
    except Exception as e:
        logger.exception("calendar_digest build error")
        return tool_error(f"Failed to build digest: {e}")

    markdown = digest_mod.render_markdown(d)

    result: Dict[str, Any] = {
        "owner": d["owner"],
        "date_str": d["date_str"],
        "events_today": len(d["today"]),
        "has_events_today": d["has_events_today"],
        "next_up_title": d["next_up"]["title"] if d.get("next_up") else None,
        "digest": markdown,
    }

    do_email = bool(args.get("email", False))
    if do_email:
        owner_email = store.get_user_email(owner)
        if not owner_email:
            result["emailed"] = False
            result["email_error"] = "no registered email for this owner"
        else:
            allowed = notify.allowed_email_recipients()
            if owner_email.lower() not in allowed:
                result["emailed"] = False
                result["email_error"] = "owner email not allowlisted"
            else:
                html_doc = digest_mod.render_html(d)
                subject = f"\U0001f4c5 Calendar digest — {d['date_str']}"
                fire_result = notify.fire(
                    "email",
                    subject,
                    markdown,
                    target=owner_email,
                    html=html_doc,
                )
                result["emailed"] = fire_result.get("ok", False)
                result["email_error"] = fire_result.get("error")

    return tool_result(result)


CALENDAR_DIGEST_SCHEMA = {
    "name": "calendar_digest",
    "description": (
        "Build and optionally email a daily digest for an owner: today's events "
        "needing attention, with statuses. When today has no events, falls back to "
        "the single closest upcoming event so the digest is always non-empty. "
        "Set email=true to send it to the owner's registered allowlisted address."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "email": {
                "type": "boolean",
                "description": (
                    "If true, email the digest to the owner's registered address "
                    "(must be in the allowlist). Default false."
                ),
            },
        },
        "required": ["owner"],
    },
}


# ---------------------------------------------------------------------------
# Job tracking tools
# ---------------------------------------------------------------------------

def _handle_calendar_list_jobs(args: Dict[str, Any], **kw) -> str:
    owner = str(args.get("owner") or "").strip()
    if not owner:
        return tool_error("owner is required")

    from_raw = args.get("from")
    to_raw = args.get("to")
    start_iso: Optional[str] = None
    end_iso: Optional[str] = None
    if from_raw and to_raw:
        from_dt = _parse_start(from_raw, recurrence_mod.DEFAULT_TZ)
        to_dt = _parse_start(to_raw, recurrence_mod.DEFAULT_TZ)
        if from_dt is None:
            return tool_error(f"Could not parse 'from': {from_raw!r}")
        if to_dt is None:
            return tool_error(f"Could not parse 'to': {to_raw!r}")
        start_iso = from_dt.astimezone(timezone.utc).isoformat()
        end_iso = to_dt.astimezone(timezone.utc).isoformat()

    try:
        jobs = store.list_jobs(owner, start_iso=start_iso, end_iso=end_iso)
    except Exception as e:
        logger.exception("calendar_list_jobs store error")
        return tool_error(f"Failed to list jobs: {e}")

    return tool_result({"owner": owner, "count": len(jobs), "jobs": jobs})


def _resolve_period_window(
    period: Optional[str],
    date_raw: Optional[str],
    tz_name: str,
) -> Optional[tuple]:
    """Compute (start_utc_iso, end_utc_iso) for a named period anchored on date_raw (or now).

    Returns None on failure. period must be one of daily/weekly/monthly/yearly.
    """
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        try:
            tz = ZoneInfo(recurrence_mod.DEFAULT_TZ)
        except Exception:
            tz = timezone.utc

    if date_raw:
        anchor_dt = _parse_start(date_raw, tz_name)
        if anchor_dt is None:
            return None
        anchor_local = anchor_dt.astimezone(tz)
    else:
        anchor_local = datetime.now(tz)

    d = anchor_local
    if period == "daily":
        start_local = d.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
    elif period == "weekly":
        # Week starts on Monday.
        start_local = (d - timedelta(days=d.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_local = start_local + timedelta(weeks=1)
    elif period == "monthly":
        import calendar as _cal
        start_local = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # First day of the month + (days in month) = first day of next month.
        last_day = _cal.monthrange(d.year, d.month)[1]
        end_local = start_local + timedelta(days=last_day)
    elif period == "yearly":
        start_local = d.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local.replace(year=d.year + 1)
    else:
        return None

    start_utc = start_local.astimezone(timezone.utc).isoformat()
    end_utc = end_local.astimezone(timezone.utc).isoformat()
    return start_utc, end_utc


def _handle_calendar_job_summary(args: Dict[str, Any], **kw) -> str:
    owner = str(args.get("owner") or "").strip()
    if not owner:
        return tool_error("owner is required")

    # Resolve timezone for the owner (use tz arg or default).
    tz_name = str(args.get("tz") or recurrence_mod.DEFAULT_TZ)
    category_raw = args.get("category")
    category = (str(category_raw).strip() or None) if category_raw is not None else None
    lang_raw = args.get("language")
    lang = str(lang_raw).strip().lower() if lang_raw else "en"
    if lang not in ("en", "fr"):
        lang = "en"

    # Resolve the time window.
    period = str(args.get("period") or "").strip().lower() or None
    date_raw = args.get("date")
    from_raw = args.get("from")
    to_raw = args.get("to")
    period_label: Optional[str] = None
    start_utc: Optional[str] = None
    end_utc: Optional[str] = None

    if period:
        result_window = _resolve_period_window(period, date_raw, tz_name)
        if result_window is None:
            return tool_error(
                f"Could not compute window for period={period!r}. "
                "Use one of: daily, weekly, monthly, yearly."
            )
        start_utc, end_utc = result_window
        period_label = period.capitalize()
        if date_raw:
            period_label += f" ({date_raw[:10]})"
    elif from_raw and to_raw:
        from_dt = _parse_start(from_raw, tz_name)
        to_dt = _parse_start(to_raw, tz_name)
        if from_dt is None:
            return tool_error(f"Could not parse 'from': {from_raw!r}")
        if to_dt is None:
            return tool_error(f"Could not parse 'to': {to_raw!r}")
        start_utc = from_dt.astimezone(timezone.utc).isoformat()
        end_utc = to_dt.astimezone(timezone.utc).isoformat()
        period_label = f"{start_utc[:10]} – {end_utc[:10]}"
    else:
        return tool_error(
            "Provide either 'period' (daily/weekly/monthly/yearly) "
            "or both 'from' and 'to' datetime strings."
        )

    try:
        summary = job_report_mod.build_job_summary(
            owner, start_utc, end_utc, tz_name,
            category=category, period_label=period_label,
        )
    except Exception as e:
        logger.exception("calendar_job_summary build error")
        return tool_error(f"Failed to build job summary: {e}")

    report_text = job_report_mod.render_text(summary)

    result: Dict[str, Any] = {
        "owner": owner,
        "period_label": period_label,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "total_seconds": summary["total_seconds"],
        "count": summary["count"],
        "jobs": summary["jobs"],
        "categories": summary["categories"],
        "report_text": report_text,
    }

    do_email = bool(args.get("email", False))
    if do_email:
        owner_email = store.get_user_email(owner)
        if not owner_email:
            result["emailed"] = False
            result["email_error"] = "no registered email for this owner"
        else:
            allowed = notify.allowed_email_recipients()
            if owner_email.lower() not in allowed:
                result["emailed"] = False
                result["email_error"] = "owner email not allowlisted"
            else:
                html_doc = job_report_mod.render_html(summary)
                pdf = job_report_mod.render_pdf(summary, lang=lang)
                subject = job_report_mod.report_subject(summary, lang=lang)
                attachments = None
                if pdf:
                    import re as _re
                    safe_owner = _re.sub(r"[^A-Za-z0-9]+", "-", owner).strip("-").lower() or "owner"
                    fname = f"job-summary-{safe_owner}.pdf"
                    attachments = [(fname, pdf, "pdf")]
                fire_result = notify.fire(
                    "email",
                    subject,
                    report_text,
                    target=owner_email,
                    attachments=attachments,
                    html=html_doc,
                )
                result["emailed"] = fire_result.get("ok", False)
                result["pdf_attached"] = bool(pdf and fire_result.get("ok"))
                result["email_error"] = fire_result.get("error")

    return tool_result(result)


CALENDAR_LIST_JOBS_SCHEMA = {
    "name": "calendar_list_jobs",
    "description": (
        "List distinct tracked jobs for an owner, each with total time, session count, "
        "category, and last-active timestamp. Useful to see which work-streams exist before "
        "asking for a detailed summary. Optionally bounded to a time window when both 'from' "
        "and 'to' are given."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "from": {
                "type": "string",
                "description": "Window start (ISO datetime). Both 'from' and 'to' must be given to apply a window.",
            },
            "to": {
                "type": "string",
                "description": "Window end (ISO datetime). Both 'from' and 'to' must be given to apply a window.",
            },
            "category": {
                "type": "string",
                "description": "Filter to a specific category (case-insensitive). Omit for all categories.",
            },
        },
        "required": ["owner"],
    },
}

CALENDAR_JOB_SUMMARY_SCHEMA = {
    "name": "calendar_job_summary",
    "description": (
        "Compute a time-tracking summary: how much total time was logged per job and per category "
        "in a given period. Use this when the user asks 'how many hours did I spend on X this "
        "week/month', 'show me my time by category for June', 'what did I work on this week', etc. "
        "Supply EITHER 'period' (daily/weekly/monthly/yearly, anchored on 'date' or now) OR explicit "
        "'from'/'to' datetime bounds. Optionally filter by 'category'. Set email=true to send a styled "
        "HTML report (with PDF if weasyprint is available) to the owner's registered email address."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "period": {
                "type": "string",
                "enum": ["daily", "weekly", "monthly", "yearly"],
                "description": (
                    "Named period containing the anchor date (or today if 'date' is omitted). "
                    "'weekly' = Monday-to-Sunday week. Mutually exclusive with 'from'/'to'."
                ),
            },
            "date": {
                "type": "string",
                "description": (
                    "Anchor date for the period, e.g. '2026-06-03'. Defaults to today. "
                    "Only used when 'period' is given."
                ),
            },
            "from": {
                "type": "string",
                "description": "Explicit window start (ISO datetime). Use with 'to'; mutually exclusive with 'period'.",
            },
            "to": {
                "type": "string",
                "description": "Explicit window end (ISO datetime). Use with 'from'; mutually exclusive with 'period'.",
            },
            "category": {
                "type": "string",
                "description": "Filter to a specific category (case-insensitive). Omit for all categories.",
            },
            "email": {
                "type": "boolean",
                "description": "If true, email the report to the owner's registered allowlisted address. Default false.",
            },
            "language": {
                "type": "string",
                "enum": ["en", "fr"],
                "description": "Language for the report/email subject. Default 'en'.",
            },
            "tz": {
                "type": "string",
                "description": (
                    f"IANA timezone for interpreting 'period' boundaries, e.g. 'Indian/Antananarivo'. "
                    f"Defaults to {recurrence_mod.DEFAULT_TZ}."
                ),
            },
        },
        "required": ["owner"],
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
    ("calendar_resume_job",   CALENDAR_RESUME_JOB_SCHEMA,   _handle_calendar_resume_job,   "▶️"),
    ("calendar_set_user_email",   CALENDAR_SET_USER_EMAIL_SCHEMA,   _handle_calendar_set_user_email,   "📧"),
    ("calendar_list_user_emails", CALENDAR_LIST_USER_EMAILS_SCHEMA, _handle_calendar_list_user_emails, "📇"),
    ("calendar_create_planning",  CALENDAR_CREATE_PLANNING_SCHEMA,  _handle_calendar_create_planning,  "📋"),
    ("calendar_list_plannings",   CALENDAR_LIST_PLANNINGS_SCHEMA,   _handle_calendar_list_plannings,   "🗒️"),
    ("calendar_get_planning",     CALENDAR_GET_PLANNING_SCHEMA,     _handle_calendar_get_planning,     "🔎"),
    ("calendar_planning_report",  CALENDAR_PLANNING_REPORT_SCHEMA,  _handle_calendar_planning_report,  "📊"),
    ("calendar_remove_planning",  CALENDAR_REMOVE_PLANNING_SCHEMA,  _handle_calendar_remove_planning,  "🗑️"),
    ("calendar_digest",           CALENDAR_DIGEST_SCHEMA,           _handle_calendar_digest,           "🗞️"),
    ("calendar_list_jobs",        CALENDAR_LIST_JOBS_SCHEMA,        _handle_calendar_list_jobs,        "🧰"),
    ("calendar_job_summary",      CALENDAR_JOB_SUMMARY_SCHEMA,      _handle_calendar_job_summary,      "📊"),
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
