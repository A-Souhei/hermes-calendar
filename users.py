"""User registry for the calendar plugin.

Reads ~/.hermes/calendar-users.json (or $CALENDAR_USERS_FILE) — a JSON file
that lists pre-registered calendar owners. Unregistered owners are refused by
creation tools (add_event, start_timer, resume_job, create_planning).

This module is deliberately dependency-light: stdlib + json ONLY.
No store / notify / tools.registry imports — so it loads cleanly inside
the dashboard's synthetic package as well as in the plugin itself.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Cache: (path, mtime) -> list[dict]. Guarded by _cache_lock because the host
# is multi-threaded (concurrent tool calls / FastAPI requests) — unsynchronized
# reads/writes (and the stale-key purge) could otherwise raise
# "dictionary changed size during iteration" or return inconsistent results.
_cache: Dict[tuple, List[Dict]] = {}
_cache_lock = threading.Lock()

_VALID_LANGUAGES = ("en", "fr")


def _registry_path() -> str:
    env_path = os.environ.get("CALENDAR_USERS_FILE")
    if env_path:
        return env_path
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    return os.path.join(hermes_home, "calendar-users.json")


def load_users() -> List[Dict]:
    """Parse the user registry JSON file and return a normalized list.

    Accepts:
      - ``{"users": [...]}`` — top-level object with a users key
      - ``[...]``            — bare top-level list

    Each entry may be:
      - a string ``"Alice"`` → normalized to ``{"name": "Alice"}``
      - an object with at least a ``name`` key

    Normalization rules:
      - ``name`` is stripped; entries with no usable name are dropped.
      - ``email`` is lowercased+stripped; absent/empty → None.
      - ``language`` kept only when it is in ``("en", "fr")``; else None.

    Results are cached by ``(path, mtime)`` so file edits are picked up on
    the next call without re-reading on every invocation. Missing file or
    parse error → ``[]`` (logged as WARNING, never raises).
    """
    global _cache
    path = _registry_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        # File does not exist — return empty, no warning (common on first run).
        return []

    cache_key = (path, mtime)
    with _cache_lock:
        if cache_key in _cache:
            return _cache[cache_key]

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:
        logger.warning("calendar users: could not load %s: %s", path, exc)
        return []

    if isinstance(raw, dict):
        entries = raw.get("users", [])
    elif isinstance(raw, list):
        entries = raw
    else:
        logger.warning("calendar users: unexpected format in %s (expected list or {users:[...]})", path)
        return []

    result: List[Dict] = []
    for entry in entries:
        if isinstance(entry, str):
            name = entry.strip()
            if not name:
                continue
            result.append({"name": name, "email": None, "language": None})
        elif isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            email_raw = entry.get("email")
            email = str(email_raw).strip().lower() if email_raw else None
            if not email:
                email = None
            lang_raw = entry.get("language")
            lang = str(lang_raw).strip().lower() if lang_raw else None
            language = lang if lang in _VALID_LANGUAGES else None
            result.append({"name": name, "email": email, "language": language})
        # else: skip unrecognized entry shapes

    # Purge any stale cache entries for this path (older mtimes) to avoid
    # unbounded growth, then write back — all under the lock so a concurrent
    # reader/writer can't observe a half-mutated dict or change its size mid-iteration.
    with _cache_lock:
        stale = [k for k in _cache if k[0] == path and k != cache_key]
        for k in stale:
            del _cache[k]
        _cache[cache_key] = result
    return result


def is_registered(name: str) -> bool:
    """Return True iff ``name`` matches a registry entry (case-insensitive).

    An empty/missing registry returns False (fail closed).
    An empty or None ``name`` returns False.
    """
    if not name:
        return False
    key = str(name).strip().lower()
    if not key:
        return False
    users = load_users()
    if not users:
        return False
    return any(str(u.get("name") or "").strip().lower() == key for u in users)


def get_user(name: str) -> Optional[Dict]:
    """Return the full registry entry for ``name`` (case-insensitive), or None."""
    if not name:
        return None
    key = str(name).strip().lower()
    for u in load_users():
        if str(u.get("name") or "").strip().lower() == key:
            return u
    return None


def registry_email(name: str) -> Optional[str]:
    """Return the registered email for ``name``, or None."""
    u = get_user(name)
    return (u.get("email") or None) if u else None


def registry_language(name: str) -> Optional[str]:
    """Return the registered language for ``name`` (``"en"`` or ``"fr"``), or None."""
    u = get_user(name)
    return (u.get("language") or None) if u else None


def list_user_names() -> List[str]:
    """Sorted list of all registered user names (for UI / help text)."""
    return sorted(str(u.get("name") or "") for u in load_users() if u.get("name"))
