"""FastAPI backend for the calendar dashboard tab.

Mounted by the Hermes dashboard at /api/plugins/calendar/ (session auth is
applied by the dashboard middleware — no auth code needed here).

Mostly read-only (GET routes that only SELECT via ``store``), plus a few
mutating POST routes wired to dashboard buttons: ``/jobs/resume``,
``/jobs/stop`` (timer actions) and ``/event/confirm`` / ``/event/cancel``
(mark one occurrence confirmed/missed + optional report). It reuses the
calendar plugin's own ``store`` and ``recurrence`` modules (loaded as a tiny
synthetic package so their relative imports resolve) — that keeps occurrence
math identical to what fires the real alerts, with no logic drift. We
deliberately avoid importing the plugin's ``__init__`` (which pulls in
``tools.registry`` / ``notify``) so this stays loadable inside the dashboard
service regardless of agent wiring.

Note: importing ``store`` runs its idempotent ``init_db()`` (CREATE TABLE IF
NOT EXISTS) on first import, so loading this module may create the DB
file/schema if it does not already exist.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()

# Cap the /events window so a huge range can't force expansion of an enormous
# recurrence set + per-event report loads (DoS guard). Mirrors /upcoming's limit.
_MAX_RANGE_DAYS = 400

# --- plugin module reuse ----------------------------------------------------

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = "calendar_dash_pkg"


def _load_plugin_modules():
    """Load the sibling store.py + recurrence.py as a minimal package."""
    if _PKG not in sys.modules:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [_PLUGIN_DIR]
        sys.modules[_PKG] = pkg

    def _sub(name: str):
        full = f"{_PKG}.{name}"
        if full in sys.modules:
            return sys.modules[full]
        path = os.path.join(_PLUGIN_DIR, f"{name}.py")
        if not os.path.exists(path):
            raise RuntimeError(f"calendar plugin module not found: {path}")
        spec = importlib.util.spec_from_file_location(full, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
        return mod

    # Load order matters: store + recurrence first (planning + timers import them);
    # users next (timers is independent of users but users is dependency-light);
    # timers last (it imports store + recurrence).
    _sub("store")
    _sub("recurrence")
    _sub("planning")
    _sub("users")
    _sub("timers")
    return (
        _sub("store"),
        _sub("recurrence"),
        _sub("planning"),
        _sub("users"),
        _sub("timers"),
    )


store, recurrence, planning, users, timers = _load_plugin_modules()


def _planning_name_for(ev: Dict[str, Any]) -> Optional[str]:
    """Planning name for an event with planning_id set, else None."""
    pid = ev.get("planning_id")
    if not pid:
        return None
    try:
        p = store.get_planning(pid)
        return p["name"] if p else None
    except Exception:
        return None


def _planning_summary(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": p["id"],
        "name": p["name"],
        "period_label": p.get("period_label"),
        "period_start_utc": p.get("period_start_utc"),
        "period_end_utc": p.get("period_end_utc"),
        "owner": p.get("owner"),
        "language": p.get("language"),
        "tz": p.get("tz"),
        "description": p.get("description"),
        "report_sent": bool(p.get("report_sent")),
        "report_sent_utc": p.get("report_sent_utc"),
    }


# --- helpers ----------------------------------------------------------------

def _event_tz(ev: Dict[str, Any]) -> ZoneInfo:
    tz_name = ev.get("tz") or recurrence.DEFAULT_TZ
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(recurrence.DEFAULT_TZ)


def _until_local_date(until_iso: str, tz_name: Optional[str]) -> str:
    """`until` (stored UTC) as a date string in the event's local tz."""
    try:
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
    """Human-readable recurrence label (kept in sync with the plugin)."""
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


def _parse_range(frm: Optional[str], to: Optional[str]):
    """Resolve the [from, to) UTC window; default = current calendar month."""
    if frm:
        try:
            start = datetime.fromisoformat(frm)
        except ValueError:
            raise HTTPException(400, f"invalid 'from': {frm}")
    else:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if to:
        try:
            end = datetime.fromisoformat(to)
        except ValueError:
            raise HTTPException(400, f"invalid 'to': {to}")
    else:
        end = start + timedelta(days=31)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end <= start:
        raise HTTPException(400, "'to' must be after 'from'")
    if (end - start) > timedelta(days=_MAX_RANGE_DAYS):
        raise HTTPException(
            400, f"range too large; keep 'to' - 'from' within {_MAX_RANGE_DAYS} days"
        )
    return start, end


def _effective_status(stored: str, occ: datetime, now: datetime, is_job: bool = False) -> str:
    """Display status: a still-floating occurrence whose time has passed reads
    as 'missed' (unconfirmed), while the STORED status stays 'floating' so it
    can still be confirmed later. Non-floating stored statuses pass through.
    A job/timer session is never 'missed' — tracked (confirmed/active) or simply
    untracked — so a floating job stays 'floating'."""
    if stored != "floating":
        return stored
    if is_job:
        return "floating"
    return "missed" if occ < now else "floating"


def _occurrences_in_range(
    start_utc: datetime,
    end_utc: datetime,
    owner: Optional[str] = None,
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for ev in store.list_events(owner=owner):
        # Category filter: skip events whose category doesn't match (case-insensitive).
        if category is not None:
            ev_cat = (ev.get("category") or "").strip().lower()
            if ev_cat != category.strip().lower():
                continue
        ev_local = dict(ev)
        try:
            ev_local["_exceptions"] = store.get_exceptions(ev["id"])
        except Exception:
            ev_local["_exceptions"] = set()
        try:
            occs = recurrence.occurrences(ev_local, start_utc, end_utc)
        except Exception:
            occs = []
        if not occs:
            continue
        tz = _event_tz(ev)
        # which occurrences already have a report?
        try:
            report_keys = {r["occurrence_utc"] for r in store.list_reports(ev["id"])}
        except Exception:
            report_keys = set()
        # Batch statuses once per event (avoids an N+1 get_status per occurrence).
        try:
            status_map = {s["occurrence_utc"]: s for s in store.list_statuses(ev["id"])}
        except Exception:
            status_map = {}
        planning_name = _planning_name_for(ev)
        is_note = ev.get("kind") == "note"
        for occ in occs:
            occ_iso = occ.isoformat()
            status_row = status_map.get(occ_iso)
            # Duration: prefer the measured (status) duration of a job/timer
            # session, else the event's planned span. This keeps job events —
            # whose duration lives only in occurrence_status — showing a span.
            ev_dur = ev.get("duration_seconds")
            status_dur = status_row.get("duration_seconds") if status_row else None
            occ_dur = status_dur if status_dur is not None else ev_dur
            # end_utc: prefer status.ended_utc, else compute from the planned span.
            ended_utc: Optional[str] = None
            if status_row and status_row.get("ended_utc"):
                ended_utc = status_row["ended_utc"]
            elif ev_dur is not None:
                try:
                    ended_utc = (occ + timedelta(seconds=ev_dur)).isoformat()
                except Exception:
                    ended_utc = None
            out.append({
                "id": ev["id"],
                "number": ev.get("seq"),
                "title": ev["title"],
                "kind": ev.get("kind", "event"),
                "occurrence_utc": occ_iso,
                "occurrence_local": occ.astimezone(tz).isoformat(),
                "end_utc": ended_utc,
                "tz": ev.get("tz") or recurrence.DEFAULT_TZ,
                "all_day": bool(ev.get("all_day")),
                "recurring": ev.get("recurrence") is not None,
                "recurrence_human": _human_recurrence(ev.get("recurrence"), ev.get("tz")),
                "alert_channel": ev.get("alert_channel"),
                "location": ev.get("location"),
                "tags": ev.get("tags") or [],
                "planning": planning_name,
                "job": ev.get("job"),
                "category": ev.get("category"),
                "has_report": occ_iso in report_keys,
                "status": "floating" if is_note else (status_row["status"] if status_row else "floating"),
                "effective_status": "floating" if is_note else _effective_status(
                    status_row["status"] if status_row else "floating", occ, now,
                    is_job=bool(ev.get("job"))
                ),
                "duration_seconds": occ_dur,
            })
    out.sort(key=lambda e: e["occurrence_utc"])
    return out


# --- routes (GET read-only, plus the POST action routes below) --------------

@router.get("/users")
def list_users():
    """Users for the filter UI: the pre-registered users from calendar-users.json
    UNIONED with any owners that actually have events/plannings — so a registered
    user appears in the dropdown even before they have any events."""
    try:
        owners = store.list_owners()
    except Exception:
        owners = []
    try:
        registered = users.list_user_names()
    except Exception:
        registered = []
    # Case-insensitive union, keeping one representative casing, sorted. Store
    # the STRIPPED name (not the raw value) so a registry entry with stray
    # leading/trailing whitespace round-trips cleanly as an `owner` filter value.
    seen: Dict[str, str] = {}
    for n in list(registered) + list(owners):
        name = (n or "").strip()
        key = name.lower()
        if key and key not in seen:
            seen[key] = name
    return {"users": sorted(seen.values(), key=lambda s: s.lower())}


@router.get("/categories")
def list_categories(owner: Optional[str] = Query(None)):
    """Distinct non-empty categories across events (for the category-filter UI)."""
    try:
        return {"categories": store.list_categories(owner=owner or None)}
    except Exception:
        return {"categories": []}


class ResumeJobRequest(BaseModel):
    owner: str
    job: str


@router.post("/jobs/resume")
def resume_job(body: ResumeJobRequest):
    """Start a new timer session resuming an existing job (same job + category).

    Auth is handled by the dashboard session-token middleware — no per-route auth.
    """
    if not body.owner or not body.owner.strip():
        raise HTTPException(400, detail="owner is required")
    if not body.job or not body.job.strip():
        raise HTTPException(400, detail="job is required")

    owner = body.owner.strip()
    job = body.job.strip()

    if not users.is_registered(owner):
        raise HTTPException(403, detail=(
            f"{owner!r} is not a registered calendar user. Users must be registered "
            "beforehand in ~/.hermes/calendar-users.json — add them there first."
        ))

    res = timers.resume_job(owner, job)
    if not res["ok"]:
        raise HTTPException(404, detail={
            "message": f"No job named {job!r} for {owner}.",
            "existing_jobs": res.get("existing_jobs", []),
        })
    return res["result"]


class StopJobRequest(BaseModel):
    event_id: str


class ConfirmEventRequest(BaseModel):
    event_id: str
    occurrence_utc: Optional[str] = None
    report: Optional[str] = None


def _apply_occurrence_outcome(body: "ConfirmEventRequest", status: str, *, reject_future: bool):
    """Shared logic for the dashboard Confirm/Cancel buttons.

    Sets one occurrence's status to `status` ('confirmed' for Confirm, 'missed'
    for Cancel) and optionally saves a report/reason. Regular events only —
    notes aren't completable and job/timer occurrences use start/stop. Auth is
    handled by the dashboard session middleware.
    """
    if not body.event_id or not body.event_id.strip():
        raise HTTPException(400, detail="event_id is required")
    eid = body.event_id.strip()
    ev = store.get_event(eid)
    if not ev:
        raise HTTPException(404, detail="event not found")
    if (ev.get("kind") or "event") == "note":
        raise HTTPException(400, detail="notes cannot be confirmed or cancelled")
    if ev.get("job"):
        raise HTTPException(400, detail="job/timer events are completed with stop, not confirm/cancel")
    occ = (body.occurrence_utc or "").strip() or ev["start_utc"]
    # Validate the occurrence key as ISO-8601 — reject a malformed one rather
    # than writing an orphan status/report row under a bad key (and so the
    # future-occurrence check below always has a real datetime to compare).
    try:
        _s = occ[:-1] + "+00:00" if occ.endswith("Z") else occ
        occ_dt = datetime.fromisoformat(_s)
        if occ_dt.tzinfo is None:
            occ_dt = occ_dt.replace(tzinfo=timezone.utc)
        occ_dt = occ_dt.astimezone(timezone.utc)
    except Exception:
        raise HTTPException(400, detail="invalid occurrence_utc")
    if reject_future and occ_dt > datetime.now(timezone.utc):
        raise HTTPException(400, detail="cannot confirm a future event")
    # Don't clobber a running timer on this occurrence.
    cur = store.get_status(eid, occ)
    if cur and cur.get("status") == "active":
        raise HTTPException(409, detail="this occurrence has a running timer; stop it first")
    # Idempotency guard: don't let the same outcome be re-applied (e.g. re-confirm
    # an already-confirmed occurrence) — that would reset the original timestamp.
    if cur and cur.get("status") == status:
        when = cur.get("updated_utc") or cur.get("created_utc")
        verb = "confirmed" if status == "confirmed" else "cancelled"
        raise HTTPException(409, detail=f"already {verb}" + (f" at {when}" if when else ""))
    store.set_status(eid, occ, status, source="dashboard")
    report_saved = False
    if body.report and body.report.strip():
        existing = store.get_report(eid, occ)
        rep = dict(existing["report"]) if existing and isinstance(existing.get("report"), dict) else {}
        rep["notes"] = body.report.strip()
        store.set_report(eid, occ, rep)
        report_saved = True
    return {"ok": True, "occurrence_utc": occ, "status": status, "report_saved": report_saved}


@router.post("/event/confirm")
def confirm_event(body: ConfirmEventRequest):
    """Confirm one occurrence of a regular event (past/now), optionally saving
    an activity report. Future occurrences are rejected (use cancel)."""
    return _apply_occurrence_outcome(body, "confirmed", reject_future=True)


@router.post("/event/cancel")
def cancel_event(body: ConfirmEventRequest):
    """Cancel one occurrence of a regular event — mark it 'missed' (it won't
    happen), optionally saving a reason. Used for future occurrences."""
    return _apply_occurrence_outcome(body, "missed", reject_future=False)


@router.post("/jobs/stop")
def stop_job(body: StopJobRequest):
    """Stop the running session of a specific event (the dashboard stop button).

    Auth is handled by the dashboard session-token middleware. Stopping an
    existing event creates nothing, so no registry check is needed here.
    """
    if not body.event_id or not body.event_id.strip():
        raise HTTPException(400, detail="event_id is required")
    res = timers.stop_event(body.event_id.strip())
    if res is None:
        raise HTTPException(404, detail="no running session for this event")
    return res


@router.get("/events")
def list_events(
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    owner: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
):
    """Expanded occurrences in [from, to) (default: current month)."""
    start, end = _parse_range(frm, to)
    return {"events": _occurrences_in_range(start, end, owner=owner or None, category=category or None)}


@router.get("/upcoming")
def upcoming(
    days: int = Query(14, ge=1, le=366),
    owner: Optional[str] = Query(None),
):
    """Expanded occurrences from now through the next ``days`` days."""
    now = datetime.now(timezone.utc)
    return {"events": _occurrences_in_range(now, now + timedelta(days=days), owner=owner or None)}


@router.get("/event/{event_id}")
def event_detail(event_id: str):
    """Full event details plus every per-occurrence report."""
    ev = store.get_event(event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    tz = _event_tz(ev)
    reports = []
    try:
        for r in store.list_reports(event_id):
            occ_iso = r["occurrence_utc"]
            try:
                occ_local = datetime.fromisoformat(occ_iso).astimezone(tz).isoformat()
            except Exception:
                occ_local = occ_iso
            reports.append({
                "occurrence_utc": occ_iso,
                "occurrence_local": occ_local,
                "report": r.get("report") or {},
                "created_utc": r.get("created_utc"),
                "updated_utc": r.get("updated_utc"),
            })
    except Exception:
        reports = []

    statuses = []
    try:
        for s in store.list_statuses(event_id):
            occ_iso = s["occurrence_utc"]
            try:
                occ_local = datetime.fromisoformat(occ_iso).astimezone(tz).isoformat()
            except Exception:
                occ_local = occ_iso
            statuses.append({
                "occurrence_utc": occ_iso,
                "occurrence_local": occ_local,
                "status": s["status"],
                "started_utc": s.get("started_utc"),
                "ended_utc": s.get("ended_utc"),
                "duration_seconds": s.get("duration_seconds"),
                "note": s.get("note"),
                "source": s.get("source"),
                "created_utc": s.get("created_utc"),
                "updated_utc": s.get("updated_utc"),
            })
    except Exception:
        statuses = []

    ev_dur = ev.get("duration_seconds")
    ev_end_utc: Optional[str] = None
    if ev_dur is not None:
        try:
            start_iso = ev.get("start_utc") or ""
            if start_iso.endswith("Z"):
                start_iso = start_iso[:-1] + "+00:00"
            start_dt_ev = datetime.fromisoformat(start_iso)
            if start_dt_ev.tzinfo is None:
                start_dt_ev = start_dt_ev.replace(tzinfo=timezone.utc)
            ev_end_utc = (start_dt_ev + timedelta(seconds=ev_dur)).isoformat()
        except Exception:
            ev_end_utc = None
    # Prefer status.ended_utc / measured duration for the resolved occurrence.
    occ_key_for_end = ev.get("start_utc") or ""
    occ_dur = ev_dur
    for s in statuses:
        if s.get("occurrence_utc") == occ_key_for_end:
            if s.get("ended_utc"):
                ev_end_utc = s["ended_utc"]
            if s.get("duration_seconds") is not None:
                occ_dur = s["duration_seconds"]
            break
    return {
        "id": ev["id"],
        "number": ev.get("seq"),
        "title": ev["title"],
        "description": ev.get("description"),
        "start_utc": ev.get("start_utc"),
        "end_utc": ev_end_utc,
        "duration_seconds": occ_dur,
        "tz": ev.get("tz") or recurrence.DEFAULT_TZ,
        "all_day": bool(ev.get("all_day")),
        "recurrence": ev.get("recurrence"),
        "recurrence_human": _human_recurrence(ev.get("recurrence"), ev.get("tz")),
        "alert_lead_seconds": ev.get("alert_lead_seconds"),
        "alert_channel": ev.get("alert_channel"),
        "language": ev.get("language"),
        "owner": ev.get("owner"),
        "notify_email": ev.get("notify_email"),
        "planning": _planning_name_for(ev),
        "kind": ev.get("kind") or "event",
        "job": ev.get("job"),
        "category": ev.get("category"),
        "meeting": ev.get("meeting"),
        "location": ev.get("location"),
        "tags": ev.get("tags") or [],
        "created_utc": ev.get("created_utc"),
        "updated_utc": ev.get("updated_utc"),
        "reports": reports,
        "statuses": statuses,
    }


@router.get("/timers")
def list_timers(owner: Optional[str] = Query(None)):
    """Active (running) timers — occurrence_status rows where status='active'.

    Optionally scoped to one owner (matching the dashboard's user filter)."""
    now = datetime.now(timezone.utc)
    rows = []
    try:
        actives = store.list_active(owner=owner or None)
    except Exception:
        actives = []
    for row in actives:
        ev = None
        try:
            ev = store.get_event(row["event_id"])
        except Exception:
            pass
        elapsed: Optional[int] = None
        started_iso = row.get("started_utc")
        if started_iso:
            try:
                started_dt = datetime.fromisoformat(started_iso)
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=timezone.utc)
                elapsed = max(0, round((now - started_dt).total_seconds()))
            except Exception:
                pass
        rows.append({
            "event_id": row["event_id"],
            "occurrence_utc": row["occurrence_utc"],
            "title": ev["title"] if ev else row["event_id"],
            "owner": ev.get("owner") if ev else None,
            "job": ev.get("job") if ev else None,
            "category": ev.get("category") if ev else None,
            "started_utc": started_iso,
            "elapsed_seconds": elapsed,
        })
    return {"timers": rows}


def _dedupe_owners(owners: List[str]) -> List[str]:
    """Distinct owners, case-insensitive, keeping one representative casing —
    so two registry spellings of the same name aren't double-counted."""
    seen: Dict[str, str] = {}
    for o in owners or []:
        name = (o or "").strip()
        key = name.lower()
        if key and key not in seen:
            seen[key] = name
    return list(seen.values())


def _worktime_logged(
    start_iso: str, end_iso: str, owner: Optional[str], category: Optional[str]
) -> int:
    """Confirmed timer-session seconds (job events only) that STARTED in
    [start_iso, end_iso) — the same started-in-window semantics the job summary
    uses. owner=None aggregates across every owner."""
    owners = [owner] if owner else _dedupe_owners(store.list_owners())
    total = 0
    for o in owners:
        try:
            total += store.summarize_jobs(o, start_iso, end_iso, category=category)["total_seconds"]
        except Exception:
            pass
    return total


def _running_sessions(
    boundaries: Dict[str, datetime], owner: Optional[str], category: Optional[str]
) -> List[Dict[str, Any]]:
    """Currently-running job sessions, each with its ``started_utc`` and which
    periods it falls into (day/week/month, by the same started-in-window rule as
    the logged total). No duration is summed here — the frontend extrapolates
    each session's live elapsed from ``started_utc`` so the cards tick every
    second between polls (a stopped session moves into the logged base instead).
    """
    cat = (category or "").strip().lower() or None
    out: List[Dict[str, Any]] = []
    try:
        actives = store.list_active(owner=owner or None)
    except Exception:
        actives = []
    for row in actives:
        started_iso = row.get("started_utc")
        if not started_iso:
            continue
        try:
            started_dt = datetime.fromisoformat(started_iso)
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=timezone.utc)
            started_dt = started_dt.astimezone(timezone.utc)
        except Exception:
            continue
        try:
            ev = store.get_event(row["event_id"])
        except Exception:
            ev = None
        if not ev or not (ev.get("job") or "").strip():
            continue
        if cat is not None and (ev.get("category") or "").strip().lower() != cat:
            continue
        out.append({
            "started_utc": started_dt.isoformat(),
            "day": started_dt >= boundaries["day"],
            "week": started_dt >= boundaries["week"],
            "month": started_dt >= boundaries["month"],
        })
    return out


@router.get("/worktime")
def worktime(
    owner: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    tz: Optional[str] = Query(None),
):
    """Total working time (job events only) for the current day, week and month.

    Periods are delimited in the caller's timezone (``tz``, IANA name; defaults
    to the plugin's DEFAULT_TZ); weeks are Monday-first to match the calendar
    grid. ``day``/``week``/``month`` are the logged (confirmed) seconds that
    started within each period — a static base. ``running`` lists the in-progress
    sessions (start + period membership) the client adds live so the cards tick
    each second between polls.
    """
    tz_name = tz or recurrence.DEFAULT_TZ
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = ZoneInfo(recurrence.DEFAULT_TZ)
        tz_name = recurrence.DEFAULT_TZ
    now_utc = datetime.now(timezone.utc)
    today = now_utc.astimezone(zone).date()
    # Build each boundary as a fresh local midnight via datetime.combine so the
    # correct (post-transition) UTC offset is picked per date — naive .replace()/
    # timedelta on an aware datetime would carry the wrong offset across a DST
    # change. Day arithmetic on date objects is pure calendar math (no DST).
    def _local_midnight(d):
        return datetime.combine(d, time(0, 0), tzinfo=zone)
    day_start = _local_midnight(today)
    week_start = _local_midnight(today - timedelta(days=today.weekday()))  # Monday-first
    month_start = _local_midnight(today.replace(day=1))
    # Upper bound just past now — sessions never start in the future, so this
    # captures everything up to the present moment for every period.
    end_iso = (now_utc + timedelta(minutes=1)).isoformat()

    cat = category or None
    boundaries = {
        "day": day_start.astimezone(timezone.utc),
        "week": week_start.astimezone(timezone.utc),
        "month": month_start.astimezone(timezone.utc),
    }
    # Logged totals are the static base; running sessions are returned separately
    # so the client can tick their live elapsed each second between polls.
    logged: Dict[str, int] = {}
    for key, start_utc in boundaries.items():
        logged[key] = _worktime_logged(start_utc.isoformat(), end_iso, owner or None, cat)
    running = _running_sessions(boundaries, owner or None, cat)

    return {
        "tz": tz_name,
        "day": logged["day"],
        "week": logged["week"],
        "month": logged["month"],
        "running": running,
    }


@router.get("/plannings")
def list_plannings(owner: Optional[str] = Query(None)):
    """All plannings, each with overall completion stats."""
    out = []
    try:
        plannings = store.list_plannings(owner=owner or None)
    except Exception:
        plannings = []
    for p in plannings:
        summary = _planning_summary(p)
        try:
            summary["overall"] = planning.planning_stats(p)["overall"]
        except Exception:
            summary["overall"] = {
                "total": 0, "confirmed": 0, "failed": 0, "completion_pct": 0,
            }
        out.append(summary)
    return {"plannings": out}


@router.get("/planning/{planning_id}")
def planning_detail(planning_id: str):
    """A planning plus its events and computed completion stats."""
    p = store.get_planning(planning_id)
    if not p:
        raise HTTPException(404, "planning not found")
    try:
        stats = planning.planning_stats(p)
    except Exception:
        stats = {"overall": {"total": 0, "confirmed": 0, "failed": 0,
                             "completion_pct": 0}, "objectives": [], "text": ""}
    events = []
    try:
        for ev in store.list_planning_events(planning_id):
            events.append({
                "id": ev["id"],
                "title": ev["title"],
                "start_utc": ev.get("start_utc"),
                "recurrence": ev.get("recurrence"),
                "recurrence_human": _human_recurrence(ev.get("recurrence"), ev.get("tz")),
                "all_day": bool(ev.get("all_day")),
            })
    except Exception:
        events = []
    return {
        **_planning_summary(p),
        "events": events,
        "overall": stats["overall"],
        "objectives": stats["objectives"],
        "report_text": stats["text"],
    }
