"""Occurrence and alert-time math for calendar events.

Uses dateutil.rrule for recurring series and zoneinfo for timezone handling.
All public functions accept and return timezone-aware UTC datetimes.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from dateutil import rrule as rrulelib

logger = logging.getLogger(__name__)

# Default timezone: configurable via env, fallback to Indian/Antananarivo
DEFAULT_TZ: str = os.environ.get("CALENDAR_TZ", "Indian/Antananarivo")

_FREQ_MAP = {
    "daily": rrulelib.DAILY,
    "weekly": rrulelib.WEEKLY,
    "monthly": rrulelib.MONTHLY,
    "yearly": rrulelib.YEARLY,
}

_WEEKDAY_MAP = [
    rrulelib.MO,
    rrulelib.TU,
    rrulelib.WE,
    rrulelib.TH,
    rrulelib.FR,
    rrulelib.SA,
    rrulelib.SU,
]


def _event_tz(event: Dict[str, Any]) -> ZoneInfo:
    tz_name = event.get("tz") or DEFAULT_TZ
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("calendar: unknown tz %r, using %s", tz_name, DEFAULT_TZ)
        return ZoneInfo(DEFAULT_TZ)


def _parse_utc(iso: str) -> datetime:
    """Parse an ISO string to an aware UTC datetime."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def occurrences(
    event: Dict[str, Any],
    range_start_utc: datetime,
    range_end_utc: datetime,
) -> List[datetime]:
    """Return all occurrence datetimes (aware UTC) in [range_start, range_end)."""
    exceptions = set(event.get("_exceptions") or [])
    start_utc = _parse_utc(event["start_utc"])
    rec = event.get("recurrence")

    if not rec:
        # One-time event
        if range_start_utc <= start_utc < range_end_utc:
            iso = start_utc.isoformat()
            if iso not in exceptions:
                return [start_utc]
        return []

    # Recurring — build rrule in event's local timezone so DST is handled
    event_tz = _event_tz(event)
    start_local = start_utc.astimezone(event_tz)

    freq = _FREQ_MAP.get(rec.get("freq", "weekly"), rrulelib.WEEKLY)
    interval = max(1, int(rec.get("interval") or 1))

    kwargs: Dict[str, Any] = {
        "freq": freq,
        "dtstart": start_local,
        "interval": interval,
    }

    byweekday = rec.get("byweekday")
    if byweekday:
        kwargs["byweekday"] = [_WEEKDAY_MAP[d] for d in byweekday if 0 <= d <= 6]

    count = rec.get("count")
    if count is not None:
        kwargs["count"] = int(count)

    until_raw = rec.get("until")
    if until_raw:
        until_dt = _parse_utc(until_raw).astimezone(event_tz)
        kwargs["until"] = until_dt

    rule = rrulelib.rrule(**kwargs)

    # Expand within a slightly padded window to catch DST edge cases
    window_start = range_start_utc.astimezone(event_tz)
    window_end = range_end_utc.astimezone(event_tz)

    result: List[datetime] = []
    for local_dt in rule.between(window_start, window_end, inc=True):
        utc_dt = local_dt.astimezone(timezone.utc)
        if utc_dt < range_start_utc or utc_dt >= range_end_utc:
            continue
        iso = utc_dt.isoformat()
        if iso in exceptions:
            continue
        result.append(utc_dt)

    return result


def next_occurrence(event: Dict[str, Any], after_utc: datetime) -> Optional[datetime]:
    """Return the next occurrence strictly after after_utc, or None."""
    # Search up to 5 years ahead
    range_end = after_utc + timedelta(days=365 * 5)
    # Inject exceptions into the event dict for occurrences()
    from .store import get_exceptions  # local import to avoid circular at module level
    ev = dict(event)
    try:
        ev["_exceptions"] = get_exceptions(event["id"])
    except Exception:
        ev["_exceptions"] = set()
    occs = occurrences(ev, after_utc + timedelta(seconds=1), range_end)
    return occs[0] if occs else None


def alert_datetime(
    event: Dict[str, Any],
    occurrence_local: datetime,
    default_lead_seconds: int,
    daily_alert_hour: int,
) -> datetime:
    """Compute the UTC datetime at which to fire the alert for this occurrence.

    Rules (in priority order):
      1. event.alert_lead_seconds set  -> occurrence - lead
      2. all_day event                 -> that occurrence date at daily_alert_hour in event tz
      3. default                       -> occurrence - default_lead_seconds
    """
    event_tz = _event_tz(event)
    occ_utc = occurrence_local.astimezone(timezone.utc)

    lead = event.get("alert_lead_seconds")
    if lead is not None:
        return occ_utc - timedelta(seconds=int(lead))

    if event.get("all_day"):
        # Fire at daily_alert_hour on the day of the occurrence, in event tz
        occ_local = occ_utc.astimezone(event_tz)
        alert_local = occ_local.replace(
            hour=daily_alert_hour, minute=0, second=0, microsecond=0
        )
        return alert_local.astimezone(timezone.utc)

    return occ_utc - timedelta(seconds=default_lead_seconds)


def due_alerts(
    event: Dict[str, Any],
    since_utc: datetime,
    now_utc: datetime,
    default_lead_seconds: int,
    daily_alert_hour: int,
) -> List[Tuple[str, datetime]]:
    """Return [(occurrence_utc_iso, alert_utc)] where alert_utc in (since_utc, now_utc].

    Expands occurrences in a window wide enough to catch alerts that fire
    before the occurrence itself (e.g. a 1-hour lead means the occurrence
    can be up to default_lead_seconds in the future).
    """
    if event.get("alert_channel") == "none":
        return []

    max_lead = max(default_lead_seconds, event.get("alert_lead_seconds") or 0)
    # Occurrences that could have alerts firing in (since_utc, now_utc]
    window_start = since_utc - timedelta(days=1)
    window_end = now_utc + timedelta(seconds=max_lead) + timedelta(days=1)

    from .store import get_exceptions  # local import to avoid circular
    ev = dict(event)
    try:
        ev["_exceptions"] = get_exceptions(event["id"])
    except Exception:
        ev["_exceptions"] = set()

    result: List[Tuple[str, datetime]] = []
    for occ_utc in occurrences(ev, window_start, window_end):
        alert_utc = alert_datetime(event, occ_utc, default_lead_seconds, daily_alert_hour)
        if since_utc < alert_utc <= now_utc:
            result.append((occ_utc.isoformat(), alert_utc))

    return result
