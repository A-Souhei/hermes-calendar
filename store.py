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
                created_utc     TEXT,
                updated_utc     TEXT
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
        """)
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
                 meeting, location, tags, created_utc, updated_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
    """Delete the event and its associated exceptions and fired alerts."""
    with _lock:
        conn = _get_conn()
        cursor = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.execute("DELETE FROM exceptions WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM fired_alerts WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM reports WHERE event_id = ?", (event_id,))
        conn.commit()
    return cursor.rowcount > 0


def list_events() -> List[Dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute("SELECT * FROM events ORDER BY start_utc").fetchall()
    return [_row_to_event(r) for r in rows]


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


# Run on import
init_db()
