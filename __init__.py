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
import os
import re
import secrets
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
from . import timers as timers_mod
from . import users as users_mod
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


def _unregistered_owner_error(owner: str) -> str:
    """Return a refusal message for an unregistered owner, naming the fix."""
    return (
        f"{owner!r} is not a registered calendar user. Users must be registered "
        "beforehand in ~/.hermes/calendar-users.json — add them there first; "
        "do not create the user here."
    )


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
    dur = ev.get("duration_seconds")
    end_utc: Optional[str] = None
    if dur is not None:
        try:
            start_iso = ev["start_utc"]
            if start_iso.endswith("Z"):
                start_iso = start_iso[:-1] + "+00:00"
            start_dt = datetime.fromisoformat(start_iso)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            end_utc = (start_dt + timedelta(seconds=dur)).isoformat()
        except Exception:
            end_utc = None
    return {
        "id": ev["id"],
        "number": ev.get("seq"),
        "title": ev["title"],
        "start_utc": ev["start_utc"],
        "end_utc": end_utc,
        "duration_seconds": dur,
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
        "kind": ev.get("kind", "event"),
    }


def _resolve_event_id(ref: Any, owner: Optional[str] = None) -> Optional[str]:
    """Resolve an event reference to a stored event id.

    Accepts a full event id (uuid hex) OR a per-owner number ('#3' / '3') —
    a number requires `owner` (the asker) to resolve. Returns the id or None.
    """
    s = str(ref or "").strip()
    if not s:
        return None
    if s.startswith("#"):
        s = s[1:].strip()
    # A 32-char hex string is an event id (uuid hex) — resolve it as such even
    # though it is digit-friendly, so an all-digit id is never mistaken for a
    # per-owner #number reference. Ids are stored lowercase, so normalize first
    # (an uppercase uuid pasted by the user would otherwise miss).
    if len(s) == 32 and all(c in "0123456789abcdef" for c in s.lower()):
        s = s.lower()
        return s if store.get_event(s) else None
    if s.isdigit():
        if not owner:
            return None
        ev = store.get_event_by_seq(str(owner).strip(), int(s))
        return ev["id"] if ev else None
    # Otherwise treat as a real id.
    return s if store.get_event(s) else None


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
    if not users_mod.is_registered(owner):
        return tool_error(_unregistered_owner_error(owner))

    category_raw = args.get("category")
    category = (str(category_raw).strip() or None) if category_raw is not None else None

    # Compute duration_seconds from `duration` or `end` args (both optional).
    duration_seconds: Optional[int] = None
    dur_raw = args.get("duration")
    end_raw = args.get("end")
    if dur_raw is not None:
        parsed_dur = _parse_lead(dur_raw)
        if parsed_dur is None:
            return tool_error(
                f"Couldn't understand duration {dur_raw!r} — use e.g. '2 hours', '90 min', '1h30m'."
            )
        if parsed_dur <= 0:
            return tool_error("duration must be positive — a real time range needs a non-zero length.")
        duration_seconds = parsed_dur
    elif end_raw is not None:
        end_dt = _parse_start(end_raw, tz_name)
        if end_dt is None:
            return tool_error(f"Could not parse end datetime: {end_raw!r}")
        dur_secs = round((end_dt.astimezone(timezone.utc) - start_dt.astimezone(timezone.utc)).total_seconds())
        if dur_secs <= 0:
            return tool_error("end must be after start")
        duration_seconds = dur_secs

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
        "duration_seconds": duration_seconds,
    }
    try:
        event_id = store.add_event(d)
    except Exception as e:
        logger.exception("calendar_add_event store error")
        return tool_error(f"Failed to save event: {e}")

    # For a one-time event that has ALREADY FINISHED, write a confirmed
    # occurrence_status (source='manual') to record actuals — mirrors how a
    # completed job is recorded. Gate on the END time (start + duration), not
    # just the start, so an event that began in the past but is still ongoing
    # is not prematurely marked confirmed.
    if duration_seconds is not None and rec is None:
        now_utc = datetime.now(timezone.utc)
        start_aware = start_dt.astimezone(timezone.utc)
        ended_aware = start_aware + timedelta(seconds=duration_seconds)
        if ended_aware <= now_utc:
            ended_utc = ended_aware.isoformat()
            try:
                store.set_status(
                    event_id, start_utc, "confirmed",
                    started_utc=start_utc,
                    ended_utc=ended_utc,
                    duration_seconds=duration_seconds,
                    source="manual",
                )
            except Exception:
                pass

    ev = store.get_event(event_id)
    return tool_result({"created": True, **_event_summary(ev)})


def _handle_calendar_update_event(args: Dict[str, Any], **kw) -> str:
    eid = _resolve_event_id(args.get("id"), owner=args.get("owner"))
    if not eid:
        return tool_error("Event not found — pass its id, or a #number together with the owner.")
    event_id = eid

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

    # add_tags / remove_tags: merge into current (or just-replaced) tags list.
    add_tags_raw = args.get("add_tags")
    remove_tags_raw = args.get("remove_tags")
    if add_tags_raw is not None or remove_tags_raw is not None:
        # Base is whatever tags will be set after the `tags` replacement above,
        # or the existing stored tags if no replacement was requested.
        base_tags: List[str] = list(
            fields["tags"] if "tags" in fields
            else (ev.get("tags") or [])
        )
        if remove_tags_raw and isinstance(remove_tags_raw, list):
            remove_lower = {str(t).strip().lower() for t in remove_tags_raw}
            base_tags = [t for t in base_tags if t.strip().lower() not in remove_lower]
        if add_tags_raw and isinstance(add_tags_raw, list):
            existing_lower = {t.strip().lower() for t in base_tags}
            for t in add_tags_raw:
                ts = str(t).strip()
                if ts and ts.lower() not in existing_lower:
                    base_tags.append(ts)
                    existing_lower.add(ts.lower())
        fields["tags"] = base_tags if base_tags else None

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

    # duration / end: recompute duration_seconds. Explicit 0 or null clears it.
    if "duration" in args or "end" in args:
        dur_raw = args.get("duration")
        end_raw = args.get("end")
        if dur_raw is not None and (isinstance(dur_raw, (int, float)) and dur_raw == 0):
            fields["duration_seconds"] = None
        elif dur_raw is not None:
            parsed_dur = _parse_lead(dur_raw)
            if parsed_dur is None:
                return tool_error(
                    f"Couldn't understand duration {dur_raw!r} — use e.g. '2 hours', '90 min', '1h30m'."
                )
            # A non-positive duration (e.g. '0' / '0 min') clears the range,
            # matching the schema's "Pass 0 or null to clear".
            fields["duration_seconds"] = parsed_dur if parsed_dur > 0 else None
        elif end_raw is not None:
            eff_tz = fields.get("tz") or ev.get("tz") or recurrence_mod.DEFAULT_TZ
            end_dt = _parse_start(end_raw, eff_tz)
            if end_dt is None:
                return tool_error(f"Could not parse end datetime: {end_raw!r}")
            start_iso = fields.get("start_utc") or ev.get("start_utc")
            if start_iso:
                try:
                    s = start_iso[:-1] + "+00:00" if start_iso.endswith("Z") else start_iso
                    start_dt_upd = datetime.fromisoformat(s)
                    if start_dt_upd.tzinfo is None:
                        start_dt_upd = start_dt_upd.replace(tzinfo=timezone.utc)
                    dur_secs = round((end_dt.astimezone(timezone.utc) - start_dt_upd.astimezone(timezone.utc)).total_seconds())
                    if dur_secs <= 0:
                        return tool_error("end must be after start")
                    fields["duration_seconds"] = dur_secs
                except Exception:
                    return tool_error("Could not compute duration from end time")
            else:
                return tool_error("Cannot compute duration: event has no start time")
        else:
            # "duration" key present but null value -> clear
            fields["duration_seconds"] = None

    if not fields:
        return tool_error("No updatable fields provided")

    try:
        updated = store.update_event(event_id, fields)
    except Exception as e:
        logger.exception("calendar_update_event store error")
        return tool_error(f"Failed to update event: {e}")

    if not updated:
        return tool_error(f"Event not found: {event_id}")

    # If duration_seconds was updated, also patch the one-time occurrence_status row
    # (best-effort: don't fail the whole update if this step errors).
    if "duration_seconds" in fields:
        try:
            new_dur = fields["duration_seconds"]
            ev_after = store.get_event(event_id)
            occ_key = ev_after["start_utc"] if ev_after else ev["start_utc"]
            existing_st = store.get_status(event_id, occ_key)
            if existing_st:
                if new_dur is None:
                    # set_status preserves None args, so rebuild the row to
                    # truly clear ended_utc/duration_seconds (keeping status,
                    # started_utc, note and source).
                    store.clear_status(event_id, occ_key)
                    store.set_status(
                        event_id, occ_key, existing_st["status"],
                        started_utc=existing_st.get("started_utc"),
                        note=existing_st.get("note"),
                        source=existing_st.get("source"),
                    )
                else:
                    ended_upd = None
                    if existing_st.get("started_utc"):
                        try:
                            s2 = existing_st["started_utc"]
                            if s2.endswith("Z"):
                                s2 = s2[:-1] + "+00:00"
                            st_dt = datetime.fromisoformat(s2)
                            if st_dt.tzinfo is None:
                                st_dt = st_dt.replace(tzinfo=timezone.utc)
                            ended_upd = (st_dt + timedelta(seconds=new_dur)).isoformat()
                        except Exception:
                            ended_upd = None
                    store.set_status(
                        event_id, occ_key, existing_st["status"],
                        ended_utc=ended_upd,
                        duration_seconds=new_dur,
                    )
        except Exception:
            pass

    ev = store.get_event(event_id)
    return tool_result({"updated": True, **_event_summary(ev)})


def _handle_calendar_remove_event(args: Dict[str, Any], **kw) -> str:
    eid = _resolve_event_id(args.get("id"), owner=args.get("owner"))
    if not eid:
        return tool_error("Event not found — pass its id, or a #number together with the owner.")
    event_id = eid

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

    # scope == "all" (default) — PERMANENT deletion of the whole event/series.
    # Force an explicit confirmation: the first call (without confirm=true)
    # returns a prompt describing exactly what will be deleted and removes
    # nothing. The agent must confirm with the user, then call again with
    # confirm=true.
    ev = store.get_event(event_id)
    if ev is None:
        return tool_error(f"Event not found: {event_id}")

    if not bool(args.get("confirm", False)):
        try:
            n_statuses = len(store.list_statuses(event_id))
        except Exception:
            n_statuses = 0
        try:
            n_reports = len(store.list_reports(event_id))
        except Exception:
            n_reports = 0
        is_rec = ev.get("recurrence") is not None
        extras: List[str] = []
        if is_rec:
            extras.append("the entire recurring series")
        if n_statuses:
            extras.append(f"{n_statuses} status/session record(s)")
        if n_reports:
            extras.append(f"{n_reports} report(s)")
        extra_txt = (" This also deletes " + ", ".join(extras) + ".") if extras else ""
        num = ev.get("seq")
        label = (f"#{num} " if num is not None else "") + (ev.get("title") or event_id)
        return tool_result({
            "needs_confirmation": True,
            "removed": False,
            "event_id": event_id,
            "number": num,
            "title": ev.get("title"),
            "recurring": is_rec,
            "message": (
                f"This will permanently delete '{label}'.{extra_txt} "
                "Confirm with the user, then call calendar_remove_event again with confirm=true."
            ),
        })

    try:
        removed = store.remove_event(event_id)
    except Exception as e:
        logger.exception("calendar_remove_event store error")
        return tool_error(f"Failed to remove event: {e}")

    if not removed:
        return tool_error(f"Event not found: {event_id}")
    return tool_result({"removed": True, "event_id": event_id})


def _handle_calendar_list_events(args: Dict[str, Any], **kw) -> str:
    from zoneinfo import ZoneInfo
    now_utc = datetime.now(timezone.utc)
    try:
        _default_tz = ZoneInfo(recurrence_mod.DEFAULT_TZ)
    except Exception:
        _default_tz = timezone.utc

    from_raw = args.get("from")
    to_raw = args.get("to")

    # NOTE: _parse_start requires (raw, tz_name); the bounds are then normalized
    # to UTC for recurrence expansion. (A missing tz_name previously crashed every
    # explicit from/to query.)
    if from_raw:
        parsed = _parse_start(from_raw, recurrence_mod.DEFAULT_TZ)
        if parsed is None:
            return tool_error(f"Could not parse 'from': {from_raw!r}")
        range_start = parsed.astimezone(timezone.utc)
    else:
        # Default to the START OF TODAY (local) — not "now" — so events earlier
        # today are still listed (you usually want to see/manage today's events).
        range_start = (
            datetime.now(_default_tz).replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
        )

    if to_raw:
        parsed = _parse_start(to_raw, recurrence_mod.DEFAULT_TZ)
        if parsed is None:
            return tool_error(f"Could not parse 'to': {to_raw!r}")
        range_end = parsed.astimezone(timezone.utc)
    else:
        range_end = now_utc + timedelta(days=30)

    if range_start > range_end:
        return tool_error("'from' must be before 'to'")

    query = str(args.get("query") or "").strip().lower()
    owner_filter = (str(args["owner"]).strip() or None) if args.get("owner") else None

    try:
        events = store.list_events(owner=owner_filter, kind="event")
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
                "number": ev.get("seq"),
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
    eid = _resolve_event_id(args.get("id"), owner=args.get("owner"))
    if not eid:
        return tool_error("Event not found — pass its id, or a #number together with the owner.")
    event_id = eid

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
        "number": ev.get("seq"),
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
        "kind": ev.get("kind", "event"),
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
    "Example: '2026-06-15T09:00:00+03:00'. All-day events can use 'YYYY-MM-DD'. "
    "Past datetimes are allowed — use them to log events that already happened."
)

CALENDAR_ADD_EVENT_SCHEMA = {
    "name": "calendar_add_event",
    "description": (
        "Add a new event to the calendar. Supports one-time and recurring events, "
        "meeting details (participants, video room URL/app), location, tags, and "
        "configurable reminders via Home Assistant push or TTS. "
        "Pass 'duration' or 'end' to give the event a time range (start–end). "
        "Past starts are allowed for logging events that already happened."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Event title (required)."},
            "start": {"type": "string", "description": _START_DESCRIPTION},
            "end": {
                "type": "string",
                "description": (
                    "Optional absolute datetime when the event ends/ended. "
                    "Used to compute duration_seconds (end - start). "
                    "Ignored when 'duration' is also given (duration wins). "
                    "Example: '2026-06-15T11:00:00+03:00'."
                ),
            },
            "duration": {
                "type": "string",
                "description": (
                    "Optional duration of the event, e.g. '2 hours', '90 min', '1h30m'. "
                    "Takes precedence over 'end' when both are given. "
                    "Omit for a point-in-time event (no time range)."
                ),
            },
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
                "type": ["string", "null"],
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
            "add_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Tags to add (merged; existing tags preserved). Applied after 'tags' "
                    "replacement if both are given. Case-insensitive dedup."
                ),
            },
            "remove_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags to remove (case-insensitive). Applied after 'tags' replacement if both given.",
            },
            "language": {
                "type": ["string", "null"],
                "enum": ["en", "fr", None],
                "description": _LANGUAGE_DESCRIPTION,
            },
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "notify_email": {"type": ["string", "null"], "description": _NOTIFY_EMAIL_DESCRIPTION},
            "category": {
                "type": ["string", "null"],
                "description": (
                    "Free-text category to group this event for reports, "
                    "e.g. 'work', 'personal', 'client-acme'. Pass null to clear it."
                ),
            },
            "end": {
                "type": ["string", "null"],
                "description": (
                    "New end datetime for the event. Recomputes duration_seconds = end - start. "
                    "Ignored when 'duration' is also given."
                ),
            },
            "duration": {
                "type": ["string", "number", "null"],
                "description": (
                    "New duration, e.g. '2 hours', '90 min'. "
                    "Pass 0 or null to clear the time range (make it a point event)."
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
        "or scope='occurrence' + occurrence date to skip a single occurrence of a recurring event. "
        "A full delete (scope='all') is PERMANENT and requires confirmation: call once WITHOUT "
        "confirm to get a summary of what will be deleted, show it to the user, and only call "
        "again with confirm=true after they explicitly agree."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Event ID or #number (e.g. '#3')."},
            "owner": {
                "type": "string",
                "description": (
                    "Owner of the event — required only to resolve a #number reference "
                    "(the asker's identifier); not needed when 'id' is a full event id."
                ),
            },
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
            "confirm": {
                "type": "boolean",
                "description": (
                    "Must be true to actually perform a permanent full delete (scope='all'). "
                    "Leave unset/false first to receive a confirmation prompt (needs_confirmation=true) "
                    "describing what will be deleted; set true only after the user has confirmed."
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
        "Defaults to the start of today … now+30 days (so events earlier today are included). "
        "ALWAYS pass 'owner' to scope results to the person asking — omit only when "
        "an admin explicitly wants to see every user's events at once."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "from": {
                "type": "string",
                "description": "Range start (ISO datetime, default: the start of today in the local timezone).",
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
            "id": {"type": "string", "description": "Event ID or #number (e.g. '#3')."},
            "owner": {
                "type": "string",
                "description": (
                    "Owner of the event — required only to resolve a #number reference "
                    "(the asker's identifier); not needed when 'id' is a full event id."
                ),
            },
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
    raw_id = _report_event_id(args)
    if not raw_id:
        return tool_error("id (the event id) is required")
    event_id = _resolve_event_id(raw_id, owner=args.get("owner"))
    if not event_id:
        return tool_error("Event not found — pass its id, or a #number together with the owner.")
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
    raw_id = _report_event_id(args)
    if not raw_id:
        return tool_error("id (the event id) is required")
    event_id = _resolve_event_id(raw_id, owner=args.get("owner"))
    if not event_id:
        return tool_error("Event not found — pass its id, or a #number together with the owner.")
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
    raw_id = _report_event_id(args)
    if not raw_id:
        return tool_error("id (the event id) is required")
    event_id = _resolve_event_id(raw_id, owner=args.get("owner"))
    if not event_id:
        return tool_error("Event not found — pass its id, or a #number together with the owner.")
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
            "id": {"type": "string", "description": "Event id or #number (e.g. '#3')."},
            "owner": {
                "type": "string",
                "description": (
                    "Owner of the event — required only to resolve a #number reference "
                    "(the asker's identifier); not needed when 'id' is a full event id."
                ),
            },
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
            "id": {"type": "string", "description": "Event id or #number (e.g. '#3')."},
            "owner": {
                "type": "string",
                "description": (
                    "Owner of the event — required only to resolve a #number reference "
                    "(the asker's identifier); not needed when 'id' is a full event id."
                ),
            },
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
        "properties": {
            "id": {"type": "string", "description": "Event id or #number (e.g. '#3')."},
            "owner": {
                "type": "string",
                "description": (
                    "Owner of the event — required only to resolve a #number reference "
                    "(the asker's identifier); not needed when 'id' is a full event id."
                ),
            },
        },
        "required": ["id"],
    },
}


# ---------------------------------------------------------------------------
# Status lifecycle + work timers
# ---------------------------------------------------------------------------

def _handle_calendar_set_status(args: Dict[str, Any], **kw) -> str:
    eid = _resolve_event_id(args.get("id"), owner=args.get("owner"))
    if not eid:
        return tool_error("Event not found — pass its id, or a #number together with the owner.")
    event_id = eid
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
    on success or (False, error_message) on failure.

    Handles all arg parsing/validation, then delegates the mechanics to
    ``timers_mod.start_session`` so the dashboard can reuse the same code path.
    """
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
    if not users_mod.is_registered(timer_owner):
        return (False, _unregistered_owner_error(timer_owner))

    notify_email_raw = args.get("notify_email")
    timer_notify_email = (
        (str(notify_email_raw).strip().lower() or None)
        if notify_email_raw is not None else None
    )

    job_raw = args.get("job")
    timer_job = (str(job_raw).strip() or None) if job_raw is not None else None

    category_raw = args.get("category")
    timer_category = (str(category_raw).strip() or None) if category_raw is not None else None

    try:
        result = timers_mod.start_session(
            owner=timer_owner,
            title=title,
            job=timer_job,
            category=timer_category,
            duration_seconds=duration_seconds,
            description=args.get("description"),
            location=args.get("location"),
            tags=tags,
            language=timer_language,
            notify_email=timer_notify_email,
            tz=tz_name,
        )
    except Exception as e:
        logger.exception("calendar_start_timer store error")
        return (False, f"Failed to create timer event: {e}")

    return (True, result)


def _handle_calendar_start_timer(args: Dict[str, Any], **kw) -> str:
    ok, payload = _start_timer_impl(args)
    return tool_result(payload) if ok else tool_error(payload)


def _log_job_impl(args: Dict[str, Any]) -> tuple:
    """Core of calendar_log_job — record a PAST job session retroactively.

    Returns (ok, payload). Validates owner (registered), job, and the time
    range (start + end|duration, in the past), then delegates to
    ``timers_mod.log_session``.
    """
    owner_raw = args.get("owner")
    owner = (str(owner_raw).strip() or None) if owner_raw is not None else None
    if not owner:
        return (False, "owner is required — log the job under the person who did it.")
    if not users_mod.is_registered(owner):
        return (False, _unregistered_owner_error(owner))

    job = str(args.get("job") or "").strip()
    if not job:
        return (False, "job is required — the name of the job/task you worked on (e.g. 'client-acme').")

    title = str(args.get("title") or "").strip() or job
    tz_name = args.get("tz") or recurrence_mod.DEFAULT_TZ

    start_raw = args.get("start")
    if not start_raw:
        return (False, "start is required — when the work began (a past datetime, e.g. '2026-06-03T14:00').")
    start_dt = _parse_start(start_raw, tz_name)
    if start_dt is None:
        return (False, f"Couldn't parse start: {start_raw!r}")

    end_raw = args.get("end")
    dur_raw = args.get("duration")
    if end_raw:
        end_dt = _parse_start(end_raw, tz_name)
        if end_dt is None:
            return (False, f"Couldn't parse end: {end_raw!r}")
    elif dur_raw is not None:
        secs = _parse_lead(dur_raw)
        if secs is None:
            return (False, f"Couldn't understand duration {dur_raw!r} — use e.g. '2 hours', '90 min', '1h30m'.")
        end_dt = start_dt + timedelta(seconds=secs)
    else:
        return (False, "Provide either 'end' (when it finished) or 'duration' (how long it took).")

    started = start_dt.astimezone(timezone.utc)
    ended = end_dt.astimezone(timezone.utc)
    if ended <= started:
        return (False, "end must be after start.")
    now = datetime.now(timezone.utc)
    if ended > now:
        return (False, "cannot log a job that ends in the future — use calendar_start_timer for ongoing work.")
    duration_seconds = max(1, round((ended - started).total_seconds()))

    tags_raw = args.get("tags")
    tags: Optional[List[str]] = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else None

    lang_raw = args.get("language")
    language: Optional[str] = None
    if lang_raw is not None:
        ll = str(lang_raw).strip().lower()
        language = ll if ll in _LANGUAGE_ENUM else None

    category_raw = args.get("category")
    category = (str(category_raw).strip() or None) if category_raw is not None else None

    try:
        result = timers_mod.log_session(
            owner=owner,
            title=title,
            started_utc=started.isoformat(),
            ended_utc=ended.isoformat(),
            duration_seconds=duration_seconds,
            job=job,
            category=category,
            description=args.get("description"),
            tags=tags,
            language=language,
            tz=tz_name,
        )
    except Exception as e:
        logger.exception("calendar_log_job store error")
        return (False, f"Failed to log job session: {e}")

    return (True, result)


def _handle_calendar_log_job(args: Dict[str, Any], **kw) -> str:
    ok, payload = _log_job_impl(args)
    return tool_result(payload) if ok else tool_error(payload)


CALENDAR_LOG_JOB_SCHEMA = {
    "name": "calendar_log_job",
    "description": (
        "Log a PAST, already-completed work session for time-tracking — use when the user "
        "says they DID some work and want it recorded retroactively (e.g. 'I worked on "
        "client-acme yesterday 2pm–4pm', 'log 3 hours on the thesis this morning'). Creates a "
        "confirmed, timer-backed job session (source=timer with started/ended/duration) that "
        "aggregates in calendar_job_summary / calendar_list_jobs, exactly like a stopped timer. "
        "For work happening RIGHT NOW use calendar_start_timer instead. Provide 'start' plus "
        "either 'end' or 'duration'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "job": {
                "type": "string",
                "description": "The job/task name worked on (e.g. 'client-acme'). Use a CONSISTENT name so sessions aggregate.",
            },
            "start": {
                "type": "string",
                "description": (
                    "When the work BEGAN — a past absolute datetime (resolve 'yesterday 2pm' to ISO "
                    "yourself), e.g. '2026-06-03T14:00:00+03:00'."
                ),
            },
            "end": {
                "type": "string",
                "description": "When the work FINISHED (absolute datetime). Provide this OR 'duration'.",
            },
            "duration": {
                "type": "string",
                "description": "How long it took, if you don't give 'end' — e.g. '2 hours', '90 min', '1h30m'.",
            },
            "title": {"type": "string", "description": "Optional short label for the session (defaults to the job name)."},
            "category": {"type": "string", "description": "Optional category for grouping (e.g. 'work', 'personal')."},
            "description": {"type": "string", "description": "Optional notes about what was done."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags."},
            "language": {
                "type": ["string", "null"],
                "enum": ["en", "fr", None],
                "description": _LANGUAGE_DESCRIPTION,
            },
            "tz": {
                "type": "string",
                "description": (
                    f"IANA timezone for interpreting start/end. Defaults to {recurrence_mod.DEFAULT_TZ}."
                ),
            },
        },
        "required": ["owner", "job", "start"],
    },
}


def _handle_calendar_resume_job(args: Dict[str, Any], **kw) -> str:
    owner_raw = args.get("owner")
    owner = (str(owner_raw).strip() or None) if owner_raw is not None else None
    if not owner:
        return tool_error("owner is required — the user this job belongs to.")
    job_raw = args.get("job")
    job = (str(job_raw).strip() or None) if job_raw is not None else None
    if not job:
        return tool_error("job is required — the name of the job to resume.")

    # Validate registry BEFORE the job-not-found path so the messages are distinct.
    if not users_mod.is_registered(owner):
        return tool_error(_unregistered_owner_error(owner))

    # Optional overrides (advertised in the schema): a fixed duration, a session
    # title, a category override, and a description for this session.
    duration_raw = args.get("duration")
    duration_seconds: Optional[int] = None
    if duration_raw is not None:
        duration_seconds = _parse_lead(duration_raw)
        if duration_seconds is None:
            return tool_error(
                f"Couldn't understand duration {duration_raw!r} — use e.g. '2 hours', "
                "'90 min', '1h30m', or omit it for an open-ended session."
            )
    title_raw = args.get("title")
    title_override = (str(title_raw).strip() or None) if title_raw is not None else None
    cat_raw = args.get("category")
    category_override = (str(cat_raw).strip() or None) if cat_raw is not None else None
    desc_raw = args.get("description")
    description_override = (str(desc_raw) or None) if desc_raw is not None else None

    res = timers_mod.resume_job(
        owner, job,
        title=title_override,
        category=category_override,
        duration_seconds=duration_seconds,
        description=description_override,
    )
    if not res["ok"]:
        names = res.get("existing_jobs", [])
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
    return tool_result(res["result"])


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

    stopped = timers_mod.stop_active_row(row, note=note)
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
            "id": {"type": "string", "description": "Event ID or #number (e.g. '#3')."},
            "owner": {
                "type": "string",
                "description": (
                    "Owner of the event — required only to resolve a #number reference "
                    "(the asker's identifier); not needed when 'id' is a full event id."
                ),
            },
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
    if not owner:
        return tool_error(
            "owner is required — every planning must belong to a user (set 'owner' "
            "to the asker, typically)."
        )
    if not users_mod.is_registered(owner):
        return tool_error(_unregistered_owner_error(owner))
    if store.get_user_email(owner) is None:
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

    category_raw = args.get("category")
    category = (str(category_raw).strip() or None) if category_raw is not None else None

    try:
        jobs = store.list_jobs(owner, start_iso=start_iso, end_iso=end_iso, category=category)
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
# Notes (alertless quick-capture entries)
# ---------------------------------------------------------------------------

def _handle_calendar_add_note(args: Dict[str, Any], **kw) -> str:
    """Create an alertless note entry stored in the calendar DB."""
    content = str(args.get("content") or "").strip()
    if not content:
        return tool_error("content is required")

    owner_raw = args.get("owner")
    owner = (str(owner_raw).strip() or None) if owner_raw is not None else None
    if not owner:
        return tool_error(
            "owner is required — every note must belong to a user. Set 'owner' "
            "to the person this note is for (typically the asker)."
        )
    if not users_mod.is_registered(owner):
        return tool_error(_unregistered_owner_error(owner))

    tz_name = args.get("tz") or recurrence_mod.DEFAULT_TZ

    when_raw = args.get("when")
    if when_raw:
        when_dt = _parse_start(when_raw, tz_name)
        if when_dt is None:
            return tool_error(f"Could not parse 'when': {when_raw!r}")
    else:
        when_dt = datetime.now(timezone.utc)

    start_utc = when_dt.astimezone(timezone.utc).isoformat()

    tags_raw = args.get("tags")
    tags: Optional[List[str]] = None
    if isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw]

    lang_raw = args.get("language")
    language: Optional[str] = None
    if lang_raw is not None:
        lang_lower = str(lang_raw).strip().lower()
        language = lang_lower if lang_lower in _LANGUAGE_ENUM else None

    d = {
        "title": content,
        "description": args.get("details"),
        "start_utc": start_utc,
        "tz": tz_name,
        "all_day": False,
        "recurrence": None,
        "alert_lead_seconds": None,
        "alert_channel": "none",
        "meeting": None,
        "location": None,
        "tags": tags,
        "language": language,
        "owner": owner,
        "notify_email": None,
        "planning_id": None,
        "kind": "note",
    }
    try:
        event_id = store.add_event(d)
    except Exception as e:
        logger.exception("calendar_add_note store error")
        return tool_error(f"Failed to save note: {e}")

    return tool_result({
        "created": True,
        "kind": "note",
        "id": event_id,
        "content": content,
        "when_utc": start_utc,
        "tags": tags,
        "owner": owner,
    })


def _handle_calendar_list_notes(args: Dict[str, Any], **kw) -> str:
    """Search and return notes for an owner, most recent first."""
    from zoneinfo import ZoneInfo

    owner_raw = args.get("owner")
    owner = (str(owner_raw).strip() or None) if owner_raw is not None else None
    if not owner:
        return tool_error("owner is required")

    try:
        notes = store.list_events(owner=owner, kind="note")
    except Exception as e:
        return tool_error(f"Failed to list notes: {e}")

    # Optional date-range filter on note timestamp (start_utc).
    from_raw = args.get("from")
    to_raw = args.get("to")
    range_start: Optional[datetime] = None
    range_end: Optional[datetime] = None
    if from_raw:
        range_start = _parse_start(from_raw, recurrence_mod.DEFAULT_TZ)
        if range_start is None:
            return tool_error(f"Could not parse 'from': {from_raw!r}")
        range_start = range_start.astimezone(timezone.utc)
    if to_raw:
        range_end = _parse_start(to_raw, recurrence_mod.DEFAULT_TZ)
        if range_end is None:
            return tool_error(f"Could not parse 'to': {to_raw!r}")
        range_end = range_end.astimezone(timezone.utc)

    # Optional substring filter over title, description, and tags.
    query = str(args.get("query") or "").strip().lower()

    result_notes = []
    for note in notes:
        # Date-range filter.
        if range_start is not None or range_end is not None:
            try:
                s = str(note["start_utc"])
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                note_dt = datetime.fromisoformat(s)
                if note_dt.tzinfo is None:
                    note_dt = note_dt.replace(tzinfo=timezone.utc)
                note_dt = note_dt.astimezone(timezone.utc)
            except Exception:
                note_dt = None
            if note_dt is not None:
                if range_start is not None and note_dt < range_start:
                    continue
                if range_end is not None and note_dt > range_end:
                    continue

        # Substring filter.
        if query:
            haystack = " ".join(filter(None, [
                note.get("title", ""),
                note.get("description", ""),
                " ".join(note.get("tags") or []),
            ])).lower()
            if query not in haystack:
                continue

        # Render when_local in the note's tz.
        tz_name = note.get("tz") or recurrence_mod.DEFAULT_TZ
        try:
            note_tz = ZoneInfo(tz_name)
            s = str(note["start_utc"])
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            when_local = dt.astimezone(note_tz).isoformat()
        except Exception:
            when_local = note["start_utc"]

        result_notes.append({
            "id": note["id"],
            "number": note.get("seq"),
            "content": note["title"],
            "details": note.get("description"),
            "when_utc": note["start_utc"],
            "when_local": when_local,
            "tags": note.get("tags"),
            "created_utc": note.get("created_utc"),
        })

    # Most recent first.
    result_notes.sort(key=lambda n: n["when_utc"], reverse=True)

    return tool_result({"count": len(result_notes), "notes": result_notes})


CALENDAR_ADD_NOTE_SCHEMA = {
    "name": "calendar_add_note",
    "description": (
        "Capture an alertless note — a quick thought or piece of information to recall later "
        "('what was that thing I noted last week?'). Notes are NEVER alerted and do NOT appear "
        "in the agenda, calendar grid, or daily digest. They are owner-scoped and searchable "
        "via calendar_list_notes. Use this when the user wants to jot something down for later "
        "recall rather than schedule an event."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The note text (required). Stored as the entry title for searching.",
            },
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "details": {
                "type": "string",
                "description": "Optional longer body / elaboration on the note.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for grouping/filtering, e.g. ['idea', 'project-x'].",
            },
            "when": {
                "type": "string",
                "description": (
                    "When the thought occurred (ISO datetime). Defaults to now. "
                    "Useful when capturing a past thought: 'I had this idea yesterday' → "
                    "pass yesterday's datetime. This timestamp is what makes "
                    "'recall notes from last week' work."
                ),
            },
            "language": {
                "type": ["string", "null"],
                "enum": ["en", "fr", None],
                "description": _LANGUAGE_DESCRIPTION,
            },
            "tz": {
                "type": "string",
                "description": (
                    f"IANA timezone for interpreting the 'when' string. "
                    f"Defaults to {recurrence_mod.DEFAULT_TZ}."
                ),
            },
        },
        "required": ["content", "owner"],
    },
}

CALENDAR_LIST_NOTES_SCHEMA = {
    "name": "calendar_list_notes",
    "description": (
        "Search an owner's notes for recall. Returns notes most-recent-first. "
        "Supports an optional text query (substring match over content, details, and tags), "
        "and optional 'from'/'to' date-range bounds over the note's timestamp "
        "(e.g. pass last Monday … today for 'what did I note last week?'). "
        "Notes never appear in the agenda or digest — use this tool to recall them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "query": {
                "type": "string",
                "description": "Substring filter applied (case-insensitive) to content, details, and tags.",
            },
            "from": {
                "type": "string",
                "description": "Lower bound on note timestamp (ISO datetime). E.g. start of last week.",
            },
            "to": {
                "type": "string",
                "description": "Upper bound on note timestamp (ISO datetime). E.g. end of last week.",
            },
        },
        "required": ["owner"],
    },
}


# ---------------------------------------------------------------------------
# General tag tool
# ---------------------------------------------------------------------------

def _handle_calendar_tag(args: Dict[str, Any], **kw) -> str:
    """Tag (or untag) any set of events by #number, uuid, or job filter."""
    owner_raw = args.get("owner")
    owner = (str(owner_raw).strip() or None) if owner_raw is not None else None
    if not owner:
        return tool_error("owner is required")
    if not users_mod.is_registered(owner):
        return tool_error(_unregistered_owner_error(owner))

    ids_raw = args.get("ids")
    job_filter = (str(args.get("job") or "").strip() or None)
    if not ids_raw and not job_filter:
        return tool_error("Provide at least one of 'ids' or 'job' to select events.")

    add_tags_raw = args.get("add_tags")
    remove_tags_raw = args.get("remove_tags")
    if not add_tags_raw and not remove_tags_raw:
        return tool_error("Provide at least one of 'add_tags' or 'remove_tags'.")

    add_tags: List[str] = [str(t).strip() for t in add_tags_raw if str(t).strip()] if add_tags_raw else []
    remove_tags: List[str] = [str(t).strip() for t in remove_tags_raw if str(t).strip()] if remove_tags_raw else []
    remove_lower = {t.lower() for t in remove_tags}

    # Build target event id set.
    target_ids: List[str] = []
    not_found: List[str] = []

    if ids_raw and isinstance(ids_raw, list):
        for ref in ids_raw:
            eid = _resolve_event_id(ref, owner=owner)
            if eid:
                if eid not in target_ids:
                    target_ids.append(eid)
            else:
                not_found.append(str(ref))

    if job_filter:
        try:
            all_evs = store.list_events(owner=owner)
        except Exception as e:
            return tool_error(f"Failed to list events: {e}")
        for ev in all_evs:
            ev_job = ev.get("job") or ""
            if ev_job.strip().lower() == job_filter.lower():
                if ev["id"] not in target_ids:
                    target_ids.append(ev["id"])

    if not target_ids and not not_found:
        return tool_result({"updated": 0, "events": [], "not_found": []})

    updated_count = 0
    updated_events = []
    for eid in target_ids:
        ev = store.get_event(eid)
        if ev is None:
            not_found.append(eid)
            continue
        current_tags: List[str] = list(ev.get("tags") or [])
        new_tags = [t for t in current_tags if t.strip().lower() not in remove_lower]
        existing_lower = {t.strip().lower() for t in new_tags}
        for t in add_tags:
            if t.lower() not in existing_lower:
                new_tags.append(t)
                existing_lower.add(t.lower())
        # Only write if something changed.
        if new_tags != current_tags:
            try:
                store.update_event(eid, {"tags": new_tags if new_tags else None})
            except Exception as e:
                logger.warning("calendar_tag: failed to update %s: %s", eid, e)
                continue
            updated_count += 1
        updated_events.append({
            "number": ev.get("seq"),
            "id": eid,
            "title": ev.get("title"),
            "job": ev.get("job"),
            "tags": new_tags if new_tags else None,
        })

    return tool_result({
        "updated": updated_count,
        "events": updated_events,
        "not_found": not_found,
    })


CALENDAR_CONVERT_TO_JOB_SCHEMA = {
    "name": "calendar_convert_to_job",
    "description": (
        "Convert a regular calendar event INTO a job session in-place — keeps the same id "
        "and #N, sets a job name, and writes a confirmed occurrence_status (source='timer') "
        "so it aggregates in calendar_job_summary / calendar_list_jobs. "
        "Requires a duration (from the event's existing duration_seconds, or an explicit "
        "'duration'/'end' override). Notes cannot be converted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "id": {
                "type": "string",
                "description": "Event ID or #number (e.g. '#3'). Requires owner to resolve a #number.",
            },
            "job": {
                "type": "string",
                "description": "Job name for the converted session. Defaults to the event's title.",
            },
            "category": {
                "type": ["string", "null"],
                "description": "Optional category override.",
            },
            "duration": {
                "type": "string",
                "description": "Override the session duration, e.g. '2 hours'. Uses the event's existing duration_seconds if omitted.",
            },
            "end": {
                "type": "string",
                "description": "Override end datetime (used to compute duration when 'duration' is omitted).",
            },
        },
        "required": ["owner", "id"],
    },
}

CALENDAR_CONVERT_TO_REGULAR_SCHEMA = {
    "name": "calendar_convert_to_regular",
    "description": (
        "Convert a job event back to a regular calendar event in-place — clears its job "
        "field and flips the occurrence_status source to 'manual' so it no longer "
        "aggregates in job summaries. Retains the same id, #N, and duration_seconds. "
        "Notes cannot be converted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "id": {
                "type": "string",
                "description": "Event ID or #number (e.g. '#3'). Requires owner to resolve a #number.",
            },
        },
        "required": ["owner", "id"],
    },
}


def _handle_calendar_convert_to_job(args: Dict[str, Any], **kw) -> str:
    owner_raw = args.get("owner")
    owner = (str(owner_raw).strip() or None) if owner_raw is not None else None
    if not owner:
        return tool_error("owner is required")
    if not users_mod.is_registered(owner):
        return tool_error(_unregistered_owner_error(owner))

    eid = _resolve_event_id(args.get("id"), owner=owner)
    if not eid:
        return tool_error("Event not found — pass its id, or a #number together with the owner.")
    ev = store.get_event(eid)
    if ev is None:
        return tool_error(f"Event not found: {eid}")
    if ev.get("kind") == "note":
        return tool_error("Notes have no time range and can't be converted.")

    # Determine duration_seconds: explicit override wins, else existing on event.
    tz_name = ev.get("tz") or recurrence_mod.DEFAULT_TZ
    dur_raw = args.get("duration")
    end_raw = args.get("end")
    if dur_raw is not None:
        dur = _parse_lead(dur_raw)
        if dur is None:
            return tool_error(
                f"Couldn't understand duration {dur_raw!r} — use e.g. '2 hours', '90 min', '1h30m'."
            )
        if dur <= 0:
            return tool_error("duration must be positive — a job session needs a non-zero length.")
    elif end_raw is not None:
        end_dt = _parse_start(end_raw, tz_name)
        if end_dt is None:
            return tool_error(f"Could not parse end: {end_raw!r}")
        start_iso = ev["start_utc"]
        if start_iso.endswith("Z"):
            start_iso = start_iso[:-1] + "+00:00"
        start_dt_ev = datetime.fromisoformat(start_iso)
        if start_dt_ev.tzinfo is None:
            start_dt_ev = start_dt_ev.replace(tzinfo=timezone.utc)
        dur = round((end_dt.astimezone(timezone.utc) - start_dt_ev.astimezone(timezone.utc)).total_seconds())
        if dur <= 0:
            return tool_error("end must be after start")
    else:
        dur = ev.get("duration_seconds")

    if dur is None:
        return tool_error("Provide a duration — this event has no time range to convert.")

    job_name = (str(args.get("job") or "").strip()) or ev["title"]
    cat_raw = args.get("category")
    category = (str(cat_raw).strip() or None) if cat_raw is not None else ev.get("category")

    start_utc = ev["start_utc"]
    if start_utc.endswith("Z"):
        start_utc = start_utc[:-1] + "+00:00"
    try:
        start_dt_ev2 = datetime.fromisoformat(start_utc)
        if start_dt_ev2.tzinfo is None:
            start_dt_ev2 = start_dt_ev2.replace(tzinfo=timezone.utc)
        ended_utc = (start_dt_ev2 + timedelta(seconds=dur)).isoformat()
    except Exception as e:
        return tool_error(f"Could not compute end time: {e}")

    update_fields: Dict[str, Any] = {
        "job": job_name,
        "duration_seconds": dur,
    }
    if cat_raw is not None:
        update_fields["category"] = category

    # Snapshot the fields we touch so we can roll back if the status write fails
    # — otherwise the event would be left half-converted (job set, but no timer
    # session), which won't aggregate correctly and is hard to diagnose.
    prior = {k: ev.get(k) for k in update_fields}

    try:
        store.update_event(eid, update_fields)
    except Exception as e:
        logger.exception("calendar_convert_to_job update error")
        return tool_error(f"Failed to update event: {e}")

    try:
        store.set_status(
            eid, start_utc, "confirmed",
            started_utc=start_utc,
            ended_utc=ended_utc,
            duration_seconds=dur,
            source="timer",
        )
    except Exception as e:
        logger.exception("calendar_convert_to_job set_status error")
        try:
            store.update_event(eid, prior)
        except Exception:
            logger.exception("calendar_convert_to_job rollback failed")
        return tool_error(f"Failed to write occurrence status: {e}")

    ev_after = store.get_event(eid)
    return tool_result({"converted_to": "job", **_event_summary(ev_after)})


def _handle_calendar_convert_to_regular(args: Dict[str, Any], **kw) -> str:
    owner_raw = args.get("owner")
    owner = (str(owner_raw).strip() or None) if owner_raw is not None else None
    if not owner:
        return tool_error("owner is required")
    if not users_mod.is_registered(owner):
        return tool_error(_unregistered_owner_error(owner))

    eid = _resolve_event_id(args.get("id"), owner=owner)
    if not eid:
        return tool_error("Event not found — pass its id, or a #number together with the owner.")
    ev = store.get_event(eid)
    if ev is None:
        return tool_error(f"Event not found: {eid}")
    if ev.get("kind") == "note":
        return tool_error("Notes have no time range and can't be converted.")

    # Preserve the span: if duration_seconds is NULL, try to recover from occurrence_status.
    dur = ev.get("duration_seconds")
    if dur is None:
        try:
            for s in store.list_statuses(eid):
                if s.get("duration_seconds") is not None:
                    dur = s["duration_seconds"]
                    break
        except Exception:
            pass

    update_fields: Dict[str, Any] = {"job": None}
    if dur is not None and ev.get("duration_seconds") is None:
        update_fields["duration_seconds"] = dur

    try:
        store.update_event(eid, update_fields)
    except Exception as e:
        logger.exception("calendar_convert_to_regular update error")
        return tool_error(f"Failed to update event: {e}")

    # Flip source='timer' status rows to source='manual' so they stop aggregating
    # as job sessions (belt-and-suspenders alongside clearing the job field).
    try:
        for s in store.list_statuses(eid):
            if s.get("source") == "timer":
                store.set_status(
                    eid, s["occurrence_utc"], s["status"],
                    started_utc=s.get("started_utc"),
                    ended_utc=s.get("ended_utc"),
                    duration_seconds=s.get("duration_seconds"),
                    source="manual",
                )
    except Exception:
        pass

    ev_after = store.get_event(eid)
    return tool_result({"converted_to": "regular", **_event_summary(ev_after)})


CALENDAR_TAG_SCHEMA = {
    "name": "calendar_tag",
    "description": (
        "Add or remove tags on ANY event kind (regular event, note, or job session) "
        "by #number, uuid, or job name — without clobbering existing tags. "
        "Works across all dates including past job sessions that calendar_list_events' "
        "default future window would miss. "
        "Examples: 'add tag urgent to #1 #2 #4', 'tag the audrey job with client', "
        "'remove tag draft from #3'. "
        "Tags are merged case-insensitively (no duplicates, originals preserved)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": _OWNER_DESCRIPTION},
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Event references to tag — each can be a #number (e.g. '#3' or '3'), "
                    "or a full event uuid. Requires owner to resolve #numbers."
                ),
            },
            "job": {
                "type": "string",
                "description": (
                    "Restrict to the owner's events whose job field matches this value "
                    "(case-insensitive). All matching events are tagged."
                ),
            },
            "add_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags to add (merged; existing tags preserved). Case-insensitive dedup.",
            },
            "remove_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags to remove (case-insensitive match).",
            },
        },
        "required": ["owner"],
    },
}


# Default download bucket for calendar_share_file. The junkyard is a
# download-only (anonymous GetObject, no listing) MinIO bucket. The upload
# endpoint (CALENDAR_BACKUP_MINIO_ENDPOINT, often plain http) differs from the
# public download base (HTTPS, e.g. fronted by a reverse proxy), so the base is
# configured separately via CALENDAR_JUNKYARD_PUBLIC_BASE (no default — set it).
_JUNKYARD_DEFAULT_BUCKET = "junkyard"


def _minio_endpoint_secure() -> tuple[Optional[str], bool]:
    """Resolve (host:port, secure) from CALENDAR_BACKUP_MINIO_* env, mirroring
    backup.py: a scheme on the endpoint wins over the SECURE flag."""
    endpoint = (os.environ.get("CALENDAR_BACKUP_MINIO_ENDPOINT") or "").strip()
    if not endpoint:
        return None, False
    secure = (os.environ.get("CALENDAR_BACKUP_MINIO_SECURE", "false")
              .strip().lower() in ("1", "true", "yes", "on"))
    if endpoint.startswith("https://"):
        endpoint, secure = endpoint[len("https://"):], True
    elif endpoint.startswith("http://"):
        endpoint, secure = endpoint[len("http://"):], False
    return endpoint.rstrip("/"), secure


def _handle_calendar_share_file(args: Dict[str, Any], **kw) -> str:
    """Publish text/a local file to the junkyard bucket; return a download URL."""
    filename = str(args.get("filename") or "").strip().strip("/")
    content = args.get("content")
    path = (str(args.get("path") or "").strip() or None)
    if content is None and not path:
        return tool_error("provide 'content' (text to publish) or 'path' (a local file to upload)")
    if path and not filename:
        filename = os.path.basename(path)
    if not filename:
        return tool_error("filename is required (the download name, e.g. 'month-backup-2026-06-toavina.txt')")

    endpoint, secure = _minio_endpoint_secure()
    # Prefer a dedicated write-only junkyard key (least privilege — it can only
    # PutObject into the junkyard); fall back to the calendar-backup creds.
    access = (os.environ.get("CALENDAR_JUNKYARD_ACCESS_KEY")
              or os.environ.get("CALENDAR_BACKUP_MINIO_ACCESS_KEY"))
    secret = (os.environ.get("CALENDAR_JUNKYARD_SECRET_KEY")
              or os.environ.get("CALENDAR_BACKUP_MINIO_SECRET_KEY"))
    if not (endpoint and access and secret):
        return tool_error(
            "junkyard upload not configured — set CALENDAR_BACKUP_MINIO_ENDPOINT plus a "
            "write key (CALENDAR_JUNKYARD_ACCESS_KEY/SECRET_KEY, or the "
            "CALENDAR_BACKUP_MINIO_ACCESS_KEY/SECRET_KEY fallback) in ~/.hermes/.env"
        )

    bucket = (os.environ.get("CALENDAR_JUNKYARD_BUCKET") or _JUNKYARD_DEFAULT_BUCKET).strip()
    prefix = (os.environ.get("CALENDAR_JUNKYARD_PREFIX") or "").strip().strip("/")
    # Unguessable token segment makes this a capability URL: the bucket serves
    # anonymous GetObject, so possession of the link is the only access grant.
    # Without it, predictable keys (e.g. month-backup-<month>-<user>.txt) would
    # be fetchable by anyone who can guess them. The human-readable filename is
    # kept as the last segment so the download still has a sensible name.
    token = secrets.token_urlsafe(12)
    object_name = "/".join(p for p in (prefix, token, filename) if p)

    if path and not os.path.isfile(path):
        return tool_error(f"path not found: {path}")

    try:
        from minio import Minio
    except ImportError:
        return tool_error("minio SDK not installed on the agent host (run: pip install minio)")

    import io
    import mimetypes
    from urllib.parse import quote

    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if ctype.startswith("text/") and "charset" not in ctype:
        ctype = f"{ctype}; charset=utf-8"

    client = Minio(endpoint, access_key=access, secret_key=secret, secure=secure)
    try:
        if path:
            size = os.path.getsize(path)
            client.fput_object(bucket, object_name, path, content_type=ctype)
        else:
            data = content if isinstance(content, bytes) else str(content).encode("utf-8")
            size = len(data)
            client.put_object(bucket, object_name, io.BytesIO(data), length=size, content_type=ctype)
    except Exception as exc:  # noqa: BLE001
        return tool_error(f"junkyard upload failed: {exc}")

    public_base = (os.environ.get("CALENDAR_JUNKYARD_PUBLIC_BASE") or "").strip().rstrip("/")
    if not public_base:
        return tool_error(
            "object uploaded but no download URL — set CALENDAR_JUNKYARD_PUBLIC_BASE "
            "(the public HTTPS base for the bucket) in ~/.hermes/.env"
        )
    # Rendered verbatim as a clickable download link, so it needs a scheme;
    # default a bare "host:port" to https so it stays a valid URL.
    if not public_base.startswith(("http://", "https://")):
        public_base = f"https://{public_base}"
    url = f"{public_base}/{quote(bucket)}/{quote(object_name)}"
    return tool_result({
        "published": True,
        "url": url,
        "bucket": bucket,
        "object": object_name,
        "bytes": size,
        "note": "passwordless download link, reachable only inside the tailnet",
    })


CALENDAR_SHARE_FILE_SCHEMA = {
    "name": "calendar_share_file",
    "description": (
        "Publish inline text or an existing local file to the MinIO 'junkyard' bucket and return a "
        "passwordless download URL. Use this INSTEAD of write_file whenever the user should receive "
        "a file as a LINK rather than a chat attachment — e.g. a month backup, an export, or a "
        "report. The link needs no login but is reachable only inside the tailnet; the bucket is "
        "download-only (not browsable). Provide either 'content' (inline text) or 'path' (an "
        "existing file), plus a 'filename' for the download. Returns {url, bucket, object, bytes}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": (
                    "Download filename / object key, e.g. 'month-backup-2026-06-toavina.txt'. "
                    "Required unless 'path' is given (then defaults to the file's basename)."
                ),
            },
            "content": {
                "type": "string",
                "description": "Inline text to publish. Provide this OR 'path'.",
            },
            "path": {
                "type": "string",
                "description": "Path to an existing local file to upload instead of inline content. Provide this OR 'content'.",
            },
        },
        "required": [],
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
    ("calendar_log_job",      CALENDAR_LOG_JOB_SCHEMA,      _handle_calendar_log_job,      "🧾"),
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
    ("calendar_add_note",         CALENDAR_ADD_NOTE_SCHEMA,         _handle_calendar_add_note,         "🗒️"),
    ("calendar_list_notes",       CALENDAR_LIST_NOTES_SCHEMA,       _handle_calendar_list_notes,       "📒"),
    ("calendar_tag",              CALENDAR_TAG_SCHEMA,              _handle_calendar_tag,              "🏷️"),
    ("calendar_convert_to_job",     CALENDAR_CONVERT_TO_JOB_SCHEMA,     _handle_calendar_convert_to_job,     "🛠️"),
    ("calendar_convert_to_regular", CALENDAR_CONVERT_TO_REGULAR_SCHEMA, _handle_calendar_convert_to_regular, "📅"),
    ("calendar_share_file",         CALENDAR_SHARE_FILE_SCHEMA,         _handle_calendar_share_file,         "🔗"),
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
