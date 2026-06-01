"""Read-only FastAPI backend for the calendar dashboard tab.

Mounted by the Hermes dashboard at /api/plugins/calendar/ (session auth is
applied by the dashboard middleware — no auth code needed here).

This module NEVER writes. It reuses the calendar plugin's own ``store`` and
``recurrence`` modules (loaded as a tiny synthetic package so their relative
imports resolve) — that keeps occurrence math identical to what fires the
real alerts, with no logic drift. We deliberately avoid importing the plugin's
``__init__`` (which pulls in ``tools.registry`` / ``notify``) so this stays
loadable inside the dashboard service regardless of agent wiring.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

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

    return _sub("store"), _sub("recurrence")


store, recurrence = _load_plugin_modules()


# --- helpers ----------------------------------------------------------------

def _event_tz(ev: Dict[str, Any]) -> ZoneInfo:
    tz_name = ev.get("tz") or recurrence.DEFAULT_TZ
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(recurrence.DEFAULT_TZ)


def _human_recurrence(rec: Optional[Dict]) -> Optional[str]:
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
        label += f" until {rec['until'][:10]}"
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
    return start, end


def _occurrences_in_range(start_utc: datetime, end_utc: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ev in store.list_events():
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
        for occ in occs:
            occ_iso = occ.isoformat()
            out.append({
                "id": ev["id"],
                "title": ev["title"],
                "occurrence_utc": occ_iso,
                "occurrence_local": occ.astimezone(tz).isoformat(),
                "tz": ev.get("tz") or recurrence.DEFAULT_TZ,
                "all_day": bool(ev.get("all_day")),
                "recurring": ev.get("recurrence") is not None,
                "recurrence_human": _human_recurrence(ev.get("recurrence")),
                "alert_channel": ev.get("alert_channel"),
                "location": ev.get("location"),
                "tags": ev.get("tags") or [],
                "has_report": occ_iso in report_keys,
            })
    out.sort(key=lambda e: e["occurrence_utc"])
    return out


# --- routes (all GET, read-only) --------------------------------------------

@router.get("/events")
def list_events(
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
):
    """Expanded occurrences in [from, to) (default: current month)."""
    start, end = _parse_range(frm, to)
    return {"events": _occurrences_in_range(start, end)}


@router.get("/upcoming")
def upcoming(days: int = Query(14, ge=1, le=366)):
    """Expanded occurrences from now through the next ``days`` days."""
    now = datetime.now(timezone.utc)
    return {"events": _occurrences_in_range(now, now + timedelta(days=days))}


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

    return {
        "id": ev["id"],
        "title": ev["title"],
        "description": ev.get("description"),
        "start_utc": ev.get("start_utc"),
        "tz": ev.get("tz") or recurrence.DEFAULT_TZ,
        "all_day": bool(ev.get("all_day")),
        "recurrence": ev.get("recurrence"),
        "recurrence_human": _human_recurrence(ev.get("recurrence")),
        "alert_lead_seconds": ev.get("alert_lead_seconds"),
        "alert_channel": ev.get("alert_channel"),
        "meeting": ev.get("meeting"),
        "location": ev.get("location"),
        "tags": ev.get("tags") or [],
        "created_utc": ev.get("created_utc"),
        "updated_utc": ev.get("updated_utc"),
        "reports": reports,
    }
