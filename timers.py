"""Shared timer mechanics for the calendar plugin.

Provides the core timer operations used by BOTH the agent tools (__init__.py)
and the dashboard backend (dashboard/plugin_api.py). Keeping the logic here
prevents drift between the two call sites (mirrors the kanban plugin philosophy).

Dependency-light: only ``store``, ``recurrence`` (relative imports), and stdlib
datetime. Does NOT import notify / tools.registry so it loads cleanly inside
the dashboard's synthetic package.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import recurrence
from . import store


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fmt_switched(s: Dict) -> str:
    """Human label for one stopped session (used in the warning string)."""
    d = s.get("duration_seconds")
    t = s.get("title") or s.get("id")
    if d is not None:
        m = max(0, round(d / 60))
        return f"'{t}' ({m}m)"
    return f"'{t}'"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def stop_active_row(row: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
    """Stop one active timer row: confirm it, record measured duration.

    ``row`` is an ``occurrence_status`` dict (as returned by ``store.list_active``).
    Returns a dict with ``id``, ``title``, ``started_utc``, ``ended_utc``,
    ``duration_seconds``.
    """
    event_id = row["event_id"]
    occ_iso = row["occurrence_utc"]
    started_iso = row.get("started_utc")

    now = datetime.now(timezone.utc)
    measured: Optional[int] = None
    if started_iso:
        try:
            # Normalize a trailing 'Z' — datetime.fromisoformat() rejects it on
            # older Pythons, which would silently drop the duration measurement.
            iso = started_iso[:-1] + "+00:00" if started_iso.endswith("Z") else started_iso
            started_dt = datetime.fromisoformat(iso)
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


def stop_event(event_id: str, note: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Stop the running (``active``) session of a SPECIFIC event, if any.

    Returns the stop result dict (id, title, started_utc, ended_utc,
    duration_seconds) or None when the event has no active session. Used by the
    dashboard's stop button, which targets one event by id.
    """
    try:
        rows = store.list_statuses(event_id)
    except Exception:
        rows = []
    for s in rows:
        if s.get("status") == "active":
            return stop_active_row(s, note=note)
    return None


def start_session(
    *,
    owner: str,
    title: str,
    job: Optional[str] = None,
    category: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    tags: Optional[List[str]] = None,
    language: Optional[str] = None,
    notify_email: Optional[str] = None,
    tz: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new timer event and set its initial status.

    Auto-switch: stops every active timer for ``owner`` before starting the new
    one and collects the stopped sessions into ``switched_from``.

    Returns the same result dict the agent tool produces:
      {id, title, started_utc, status}
      + duration_seconds  (only when fixed)
      + switched_from     (list, only when at least one was stopped)
      + warning           (human string, only when switched_from is non-empty)

    This function does NOT validate the registry — callers are responsible for
    ensuring the owner is registered (``users.is_registered``) before calling.
    """
    tz_name = tz or recurrence.DEFAULT_TZ
    now = datetime.now(timezone.utc)

    # Auto-switch: stop any running timers for this owner.
    existing_actives = store.list_active(owner=owner)
    switched_from: List[Dict[str, Any]] = []
    for active_row in existing_actives:
        stopped = stop_active_row(active_row)
        switched_from.append({
            "id": stopped["id"],
            "title": stopped["title"],
            "duration_seconds": stopped["duration_seconds"],
        })

    event_data: Dict[str, Any] = {
        "title": title,
        "description": description,
        "start_utc": now.isoformat(),
        "tz": tz_name,
        "all_day": False,
        "recurrence": None,
        "alert_lead_seconds": None,
        "alert_channel": "none",
        "meeting": None,
        "location": location,
        "tags": tags,
        "language": language,
        "owner": owner,
        "notify_email": notify_email,
        "job": job,
        "category": category,
    }
    event_id = store.add_event(event_data)
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
        stopped_labels = ", ".join(_fmt_switched(s) for s in switched_from)
        result["warning"] = f"Stopped running job {stopped_labels} and started '{title}'."

    return result


def log_session(
    *,
    owner: str,
    title: str,
    started_utc: str,
    ended_utc: str,
    duration_seconds: int,
    job: Optional[str] = None,
    category: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    tags: Optional[List[str]] = None,
    language: Optional[str] = None,
    notify_email: Optional[str] = None,
    tz: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a PAST, already-finished timer-backed job session.

    Unlike ``start_session`` (which begins at *now*), this logs a completed
    session retroactively from explicit ``started_utc``/``ended_utc`` — for
    logging work that was already done. It creates a one-time, alertless event
    anchored at ``started_utc`` and a 'confirmed' occurrence_status carrying
    ``source='timer'`` + started/ended/duration — the SAME shape a stopped
    timer produces, so it aggregates in ``summarize_jobs`` / ``list_jobs``.

    Does NOT auto-switch running timers (logging past work is independent of any
    live session) and does NOT validate the registry — callers do.
    """
    tz_name = tz or recurrence.DEFAULT_TZ
    event_data: Dict[str, Any] = {
        "title": title,
        "description": description,
        "start_utc": started_utc,
        "tz": tz_name,
        "all_day": False,
        "recurrence": None,
        "alert_lead_seconds": None,
        "alert_channel": "none",
        "meeting": None,
        "location": location,
        "tags": tags,
        "language": language,
        "owner": owner,
        "notify_email": notify_email,
        "job": job,
        "category": category,
    }
    event_id = store.add_event(event_data)
    # Occurrence key = the event's own start (one-time event), matching how
    # start_session/stop key the status row.
    store.set_status(
        event_id, started_utc, "confirmed",
        started_utc=started_utc,
        ended_utc=ended_utc,
        duration_seconds=duration_seconds,
        source="timer",
    )
    return {
        "id": event_id,
        "title": title,
        "job": job,
        "category": category,
        "started_utc": started_utc,
        "ended_utc": ended_utc,
        "duration_seconds": duration_seconds,
        "status": "confirmed",
        "logged": True,
    }


def resume_job(
    owner: str,
    job: str,
    *,
    title: Optional[str] = None,
    category: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Resume the most recent session of a job for ``owner``.

    Reuses the matched job's EXACT stored ``job`` name (so sessions aggregate)
    and, by default, its title/category; explicit overrides win when provided.

    Returns:
      ``{"ok": False, "reason": "not_found", "existing_jobs": [...]}``
        when no matching job event is found.
      ``{"ok": True, "result": {..., "resumed": True, "resumed_from": {...}}}``
        on success (the inner result has the same shape as ``start_session``).

    Does NOT validate the registry — callers validate the owner.
    """
    m = store.find_job_event(owner, job)
    if m is None:
        try:
            existing = store.list_jobs(owner)
        except Exception:
            existing = []
        existing_jobs = sorted({j["job"] for j in existing if j.get("job")})
        return {"ok": False, "reason": "not_found", "existing_jobs": existing_jobs}

    session = start_session(
        owner=owner,
        title=title or m.get("title") or m.get("job") or job,
        job=m.get("job"),
        category=category if category is not None else m.get("category"),
        duration_seconds=duration_seconds,
        description=description,
    )
    session["resumed"] = True
    session["resumed_from"] = {
        "job": m.get("job"),
        "category": m.get("category"),
        "last_session_utc": m.get("start_utc"),
    }
    return {"ok": True, "result": session}
