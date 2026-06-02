"""SQLite storage layer for the calendar plugin.

DB path: $HERMES_HOME/calendar.db  (default ~/.hermes/calendar.db)
Uses WAL mode and a module-level lock so callers need no extra locking.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(
    os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
    "calendar.db",
)

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def init_db() -> None:
    """Create tables if they do not exist. Called on module import."""
    with _lock:
        conn = _get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id              TEXT PRIMARY KEY,
                title           TEXT NOT NULL,
                description     TEXT,
                start_utc       TEXT NOT NULL,
                tz              TEXT,
                all_day         INTEGER DEFAULT 0,
                recurrence      TEXT,
                alert_lead_seconds INTEGER,
                alert_channel   TEXT,
                meeting         TEXT,
                location        TEXT,
                tags            TEXT,
                language        TEXT,
                owner           TEXT,
                notify_email    TEXT,
                planning_id     TEXT,
                created_utc     TEXT,
                updated_utc     TEXT
            );

            CREATE TABLE IF NOT EXISTS plannings (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                period_label    TEXT,
                period_start_utc TEXT NOT NULL,
                period_end_utc  TEXT NOT NULL,
                owner           TEXT,
                language        TEXT,
                description     TEXT,
                report_sent     INTEGER DEFAULT 0,
                report_sent_utc TEXT,
                created_utc     TEXT,
                updated_utc     TEXT
            );

            CREATE TABLE IF NOT EXISTS user_emails (
                name        TEXT PRIMARY KEY,
                email       TEXT NOT NULL,
                created_utc TEXT,
                updated_utc TEXT
            );

            CREATE TABLE IF NOT EXISTS exceptions (
                event_id        TEXT NOT NULL,
                occurrence_utc  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fired_alerts (
                event_id        TEXT NOT NULL,
                occurrence_utc  TEXT NOT NULL,
                fired_utc       TEXT NOT NULL,
                PRIMARY KEY (event_id, occurrence_utc)
            );

            CREATE TABLE IF NOT EXISTS reports (
                event_id        TEXT NOT NULL,
                occurrence_utc  TEXT NOT NULL,
                report          TEXT NOT NULL,
                created_utc     TEXT,
                updated_utc     TEXT,
                PRIMARY KEY (event_id, occurrence_utc)
            );

            CREATE TABLE IF NOT EXISTS occurrence_status (
                event_id         TEXT NOT NULL,
                occurrence_utc   TEXT NOT NULL,
                status           TEXT NOT NULL,
                started_utc      TEXT,
                ended_utc        TEXT,
                duration_seconds INTEGER,
                note             TEXT,
                source           TEXT,
                created_utc      TEXT,
                updated_utc      TEXT,
                PRIMARY KEY (event_id, occurrence_utc)
            );
        """)
        conn.commit()
        # Idempotent migration: add language column to existing DBs that
        # were created before this column was introduced.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "language" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN language TEXT")
            conn.commit()
        if "owner" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN owner TEXT")
            conn.commit()
        if "notify_email" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN notify_email TEXT")
            conn.commit()
        if "planning_id" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN planning_id TEXT")
            conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_event(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for field in ("recurrence", "meeting", "tags"):
        raw = d.get(field)
        if raw is not None:
            try:
                d[field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d[field] = None
        else:
            d[field] = None
    d["all_day"] = bool(d.get("all_day", 0))
    return d


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def add_event(d: Dict[str, Any]) -> str:
    """Insert a new event; returns its generated id (uuid4 hex)."""
    event_id = uuid.uuid4().hex
    now = _now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO events
                (id, title, description, start_utc, tz, all_day,
                 recurrence, alert_lead_seconds, alert_channel,
                 meeting, location, tags, language, owner, notify_email,
                 planning_id, created_utc, updated_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                d["title"],
                d.get("description"),
                d["start_utc"],
                d.get("tz"),
                int(bool(d.get("all_day", False))),
                json.dumps(d["recurrence"]) if d.get("recurrence") is not None else None,
                d.get("alert_lead_seconds"),
                d.get("alert_channel"),
                json.dumps(d["meeting"]) if d.get("meeting") is not None else None,
                d.get("location"),
                json.dumps(d["tags"]) if d.get("tags") is not None else None,
                d.get("language"),
                d.get("owner"),
                d.get("notify_email"),
                d.get("planning_id"),
                now,
                now,
            ),
        )
        conn.commit()
    return event_id


def get_event(event_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
    return _row_to_event(row) if row else None


def update_event(event_id: str, fields: Dict[str, Any]) -> bool:
    """Update only the provided keys. Bumps updated_utc. Returns False if not found."""
    if not fields:
        return False
    # Serialize JSON fields
    serialized: Dict[str, Any] = {}
    for key, val in fields.items():
        if key in ("recurrence", "meeting", "tags"):
            serialized[key] = json.dumps(val) if val is not None else None
        elif key == "all_day":
            serialized[key] = int(bool(val))
        else:
            serialized[key] = val
    serialized["updated_utc"] = _now_iso()

    set_clause = ", ".join(f"{k} = ?" for k in serialized)
    values = list(serialized.values()) + [event_id]
    with _lock:
        conn = _get_conn()
        cursor = conn.execute(
            f"UPDATE events SET {set_clause} WHERE id = ?", values
        )
        conn.commit()
    return cursor.rowcount > 0


def remove_event(event_id: str) -> bool:
    """Delete the event and its associated exceptions, fired alerts, reports, and statuses."""
    with _lock:
        conn = _get_conn()
        cursor = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.execute("DELETE FROM exceptions WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM fired_alerts WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM reports WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM occurrence_status WHERE event_id = ?", (event_id,))
        conn.commit()
    return cursor.rowcount > 0


def list_events() -> List[Dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute("SELECT * FROM events ORDER BY start_utc").fetchall()
    return [_row_to_event(r) for r in rows]


def list_planning_events(planning_id: str) -> List[Dict[str, Any]]:
    """Return all events tagged with the given planning_id, ordered by start."""
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM events WHERE planning_id = ? ORDER BY start_utc",
            (planning_id,),
        ).fetchall()
    return [_row_to_event(r) for r in rows]


# ---------------------------------------------------------------------------
# Plannings (named, period-bounded sets of events)
# ---------------------------------------------------------------------------

def add_planning(d: Dict[str, Any]) -> str:
    """Insert a new planning; returns its generated id (uuid4 hex)."""
    planning_id = uuid.uuid4().hex
    now = _now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO plannings
                (id, name, period_label, period_start_utc, period_end_utc,
                 owner, language, description, report_sent, report_sent_utc,
                 created_utc, updated_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                planning_id,
                d["name"],
                d.get("period_label"),
                d["period_start_utc"],
                d["period_end_utc"],
                d.get("owner"),
                d.get("language"),
                d.get("description"),
                int(bool(d.get("report_sent", 0))),
                d.get("report_sent_utc"),
                now,
                now,
            ),
        )
        conn.commit()
    return planning_id


def get_planning(planning_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM plannings WHERE id = ?", (planning_id,)
        ).fetchone()
    return dict(row) if row else None


def get_planning_by_name(name: str) -> Optional[Dict[str, Any]]:
    """Case-insensitive lookup by name; returns the most recent if duplicates."""
    key = str(name).strip().lower()
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM plannings WHERE LOWER(name) = ? "
            "ORDER BY created_utc DESC LIMIT 1",
            (key,),
        ).fetchone()
    return dict(row) if row else None


def list_plannings() -> List[Dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM plannings ORDER BY period_start_utc"
        ).fetchall()
    return [dict(r) for r in rows]


def update_planning(planning_id: str, fields: Dict[str, Any]) -> bool:
    """Update only the provided keys. Bumps updated_utc. Returns False if no row matched."""
    if not fields:
        return False
    serialized = dict(fields)
    if "report_sent" in serialized:
        serialized["report_sent"] = int(bool(serialized["report_sent"]))
    serialized["updated_utc"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in serialized)
    values = list(serialized.values()) + [planning_id]
    with _lock:
        conn = _get_conn()
        cursor = conn.execute(
            f"UPDATE plannings SET {set_clause} WHERE id = ?", values
        )
        conn.commit()
    return cursor.rowcount > 0


def set_report_sent(planning_id: str) -> None:
    """Mark a planning's end-of-period report as sent (dedup guard)."""
    now = _now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE plannings SET report_sent = 1, report_sent_utc = ?, "
            "updated_utc = ? WHERE id = ?",
            (now, now, planning_id),
        )
        conn.commit()


def remove_planning(planning_id: str, remove_events: bool = False) -> bool:
    """Delete the planning row. If remove_events: delete each of its events
    (cascading their statuses/reports/exceptions via remove_event); else
    detach the events by clearing their planning_id. Returns True if the
    planning row was deleted."""
    if remove_events:
        for ev in list_planning_events(planning_id):
            remove_event(ev["id"])
    else:
        with _lock:
            conn = _get_conn()
            conn.execute(
                "UPDATE events SET planning_id = NULL WHERE planning_id = ?",
                (planning_id,),
            )
            conn.commit()
    with _lock:
        conn = _get_conn()
        cursor = conn.execute("DELETE FROM plannings WHERE id = ?", (planning_id,))
        conn.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# User email registry (name -> email, for email-channel reminders)
# ---------------------------------------------------------------------------

def set_user_email(name: str, email: str) -> None:
    """Upsert a person's email association. The name (lowercased+stripped) is
    the key; the email is stored lowercased+stripped. created_utc is preserved
    on conflict; updated_utc is always bumped."""
    key = str(name).strip().lower()
    addr = str(email).strip().lower()
    now = _now_iso()
    with _lock:
        conn = _get_conn()
        existing = conn.execute(
            "SELECT created_utc FROM user_emails WHERE name = ?", (key,)
        ).fetchone()
        created = existing["created_utc"] if existing else now
        conn.execute(
            """INSERT INTO user_emails (name, email, created_utc, updated_utc)
               VALUES (?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 email       = excluded.email,
                 updated_utc = excluded.updated_utc""",
            (key, addr, created, now),
        )
        conn.commit()


def get_user_email(name: str) -> Optional[str]:
    """Return the registered email for a name (lookup by lowercased name), or None."""
    key = str(name).strip().lower()
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT email FROM user_emails WHERE name = ?", (key,)
        ).fetchone()
    return row["email"] if row else None


def list_user_emails() -> List[Dict[str, Any]]:
    """Return all name -> email associations."""
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM user_emails ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def remove_user_email(name: str) -> bool:
    """Delete a name's email association. Returns True if a row was deleted."""
    key = str(name).strip().lower()
    with _lock:
        conn = _get_conn()
        cursor = conn.execute("DELETE FROM user_emails WHERE name = ?", (key,))
        conn.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Exceptions (skipped occurrences)
# ---------------------------------------------------------------------------

def add_exception(event_id: str, occurrence_utc_iso: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO exceptions (event_id, occurrence_utc) VALUES (?, ?)",
            (event_id, occurrence_utc_iso),
        )
        conn.commit()


def get_exceptions(event_id: str) -> Set[str]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT occurrence_utc FROM exceptions WHERE event_id = ?", (event_id,)
        ).fetchall()
    return {r["occurrence_utc"] for r in rows}


# ---------------------------------------------------------------------------
# Fired alerts (deduplication)
# ---------------------------------------------------------------------------

def was_fired(event_id: str, occurrence_iso: str) -> bool:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT 1 FROM fired_alerts WHERE event_id = ? AND occurrence_utc = ?",
            (event_id, occurrence_iso),
        ).fetchone()
    return row is not None


def mark_fired(event_id: str, occurrence_iso: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO fired_alerts (event_id, occurrence_utc, fired_utc) VALUES (?,?,?)",
            (event_id, occurrence_iso, _now_iso()),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Reports (per-occurrence minutes / transcription / notes)
# ---------------------------------------------------------------------------

def set_report(event_id: str, occurrence_utc: str, report: Dict[str, Any]) -> None:
    """Create or replace the report for one occurrence of an event (preserves created_utc)."""
    now = _now_iso()
    with _lock:
        conn = _get_conn()
        existing = conn.execute(
            "SELECT created_utc FROM reports WHERE event_id = ? AND occurrence_utc = ?",
            (event_id, occurrence_utc),
        ).fetchone()
        created = existing["created_utc"] if existing else now
        conn.execute(
            """INSERT INTO reports (event_id, occurrence_utc, report, created_utc, updated_utc)
               VALUES (?,?,?,?,?)
               ON CONFLICT(event_id, occurrence_utc)
               DO UPDATE SET report = excluded.report, updated_utc = excluded.updated_utc""",
            (event_id, occurrence_utc, json.dumps(report), created, now),
        )
        conn.commit()


def _row_to_report(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["report"] = json.loads(d["report"])
    except (json.JSONDecodeError, TypeError):
        d["report"] = {}
    return d


def get_report(event_id: str, occurrence_utc: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM reports WHERE event_id = ? AND occurrence_utc = ?",
            (event_id, occurrence_utc),
        ).fetchone()
    return _row_to_report(row) if row else None


def list_reports(event_id: str) -> List[Dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM reports WHERE event_id = ? ORDER BY occurrence_utc",
            (event_id,),
        ).fetchall()
    return [_row_to_report(r) for r in rows]


# ---------------------------------------------------------------------------
# Occurrence status (per-occurrence lifecycle: floating / active / confirmed / missed)
# ---------------------------------------------------------------------------

def set_status(
    event_id: str,
    occurrence_utc: str,
    status: str,
    *,
    started_utc: Optional[str] = None,
    ended_utc: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    note: Optional[str] = None,
    source: Optional[str] = None,
) -> None:
    """Upsert the status row for one occurrence.

    Partial-update semantics: only fields whose argument is not None are
    overwritten — existing started/ended/duration/note/source are preserved
    when None is passed, so callers can update a single field safely.
    created_utc is preserved on conflict; updated_utc is always bumped.
    """
    now = _now_iso()
    with _lock:
        conn = _get_conn()
        existing = conn.execute(
            "SELECT * FROM occurrence_status WHERE event_id = ? AND occurrence_utc = ?",
            (event_id, occurrence_utc),
        ).fetchone()
        if existing:
            created = existing["created_utc"] or now
            merged_started = started_utc if started_utc is not None else existing["started_utc"]
            merged_ended = ended_utc if ended_utc is not None else existing["ended_utc"]
            merged_duration = duration_seconds if duration_seconds is not None else existing["duration_seconds"]
            merged_note = note if note is not None else existing["note"]
            merged_source = source if source is not None else existing["source"]
        else:
            created = now
            merged_started = started_utc
            merged_ended = ended_utc
            merged_duration = duration_seconds
            merged_note = note
            merged_source = source
        conn.execute(
            """INSERT INTO occurrence_status
                (event_id, occurrence_utc, status, started_utc, ended_utc,
                 duration_seconds, note, source, created_utc, updated_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id, occurrence_utc) DO UPDATE SET
                 status           = excluded.status,
                 started_utc      = excluded.started_utc,
                 ended_utc        = excluded.ended_utc,
                 duration_seconds = excluded.duration_seconds,
                 note             = excluded.note,
                 source           = excluded.source,
                 updated_utc      = excluded.updated_utc""",
            (
                event_id, occurrence_utc, status,
                merged_started, merged_ended, merged_duration,
                merged_note, merged_source, created, now,
            ),
        )
        conn.commit()


def get_status(event_id: str, occurrence_utc: str) -> Optional[Dict[str, Any]]:
    """Return the status row for one occurrence, or None (meaning 'floating')."""
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM occurrence_status WHERE event_id = ? AND occurrence_utc = ?",
            (event_id, occurrence_utc),
        ).fetchone()
    return dict(row) if row else None


def list_statuses(event_id: str) -> List[Dict[str, Any]]:
    """Return all status rows for an event, ordered by occurrence_utc."""
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM occurrence_status WHERE event_id = ? ORDER BY occurrence_utc",
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_status(event_id: str, occurrence_utc: str) -> bool:
    """Delete the status row (resets the occurrence to 'floating'). Returns True if a row was deleted."""
    with _lock:
        conn = _get_conn()
        cursor = conn.execute(
            "DELETE FROM occurrence_status WHERE event_id = ? AND occurrence_utc = ?",
            (event_id, occurrence_utc),
        )
        conn.commit()
    return cursor.rowcount > 0


def list_active() -> List[Dict[str, Any]]:
    """Return all occurrence_status rows where status='active', across all events."""
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM occurrence_status WHERE status = 'active' ORDER BY started_utc",
        ).fetchall()
    return [dict(r) for r in rows]


# Run on import
init_db()
