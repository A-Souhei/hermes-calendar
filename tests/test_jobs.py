"""Self-contained tests for the realtime job time-tracking feature.

Runs without the Hermes host: it stubs the ``tools.registry`` module that the
plugin imports and loads the plugin package under a synthetic name against a
throwaway DB (``HERMES_HOME`` points at a temp dir). Run directly:

    python tests/test_jobs.py

or under pytest (the ``test_*`` functions are discovered automatically):

    pytest tests/test_jobs.py

Requires python-dateutil (a plugin runtime dependency). weasyprint is optional;
PDF rendering is not exercised here.
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# --- Registry: set up CALENDAR_USERS_FILE FIRST so the plugin sees it -------
# ---------------------------------------------------------------------------

_tmpdir = tempfile.mkdtemp(prefix="caltest_")

# --- isolate storage to a throwaway DB BEFORE the plugin imports store -------
os.environ["HERMES_HOME"] = _tmpdir

# Write a registry file covering EVERY owner used across the test suite.
_registry_path = os.path.join(_tmpdir, "calendar-users.json")
_registry_data = {
    "users": [
        {"name": "Toavina",   "email": "t@example.com",      "language": "en"},
        {"name": "u_persist", "email": "persist@example.com", "language": "en"},
        {"name": "u_switch",  "email": "switch@example.com",  "language": "en"},
        {"name": "u_other",   "email": "other@example.com",   "language": "en"},
        {"name": "u_sum",     "email": "sum@example.com",     "language": "en"},
        {"name": "u_resume",  "email": "resume@example.com",  "language": "en"},
        {"name": "u_upd",     "email": "upd@example.com",     "language": "en"},
        {"name": "u_cat",     "email": "cat@example.com",     "language": "en"},
        {"name": "u_reg",     "email": "reg@example.com",     "language": "en"},
        {"name": "u_plan",    "email": "plan@example.com",    "language": "en"},
        {"name": "u_note",     "email": "note@example.com",     "language": "en"},
        {"name": "u_notelist", "email": "notelist@example.com", "language": "en"},
        {"name": "u_noteflt",  "email": "noteflt@example.com",  "language": "en"},
        {"name": "u_log",      "email": "log@example.com",      "language": "en"},
        {"name": "u_noemail"},  # registered but no email (for the planning gate test)
    ]
}
with open(_registry_path, "w", encoding="utf-8") as _f:
    json.dump(_registry_data, _f)

os.environ["CALENDAR_USERS_FILE"] = _registry_path

# --- stub the host runtime the plugin's __init__ imports ---------------------
_tools = types.ModuleType("tools")
_reg = types.ModuleType("tools.registry")
_reg.tool_result = lambda d: json.dumps({"ok": True, "result": d})
_reg.tool_error = lambda m: json.dumps({"ok": False, "error": m})
_tools.registry = _reg
sys.modules.setdefault("tools", _tools)
sys.modules.setdefault("tools.registry", _reg)

# --- load the plugin package under a NON-'calendar' synthetic name -----------
# (named 'calendar' would shadow the stdlib calendar module the plugin uses).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = "calendar_plugin_under_test"


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        _PKG, os.path.join(_ROOT, "__init__.py"),
        submodule_search_locations=[_ROOT],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = mod
    spec.loader.exec_module(mod)
    return mod


cal = _load_plugin()
store = cal.store
timers = cal.timers_mod


def res(s):
    """Unwrap a stubbed tool_result; raise on a tool_error envelope."""
    o = json.loads(s)
    if not o.get("ok"):
        raise AssertionError("tool error: " + o.get("error", "?"))
    return o["result"]


def _start(owner, **kw):
    return res(cal._handle_calendar_start_timer({"owner": owner, **kw}))


# --- tests -------------------------------------------------------------------

def test_migration_columns():
    cols = {r[1] for r in store._get_conn().execute("PRAGMA table_info(events)")}
    assert "job" in cols and "category" in cols


def test_start_timer_persists_job_and_category():
    r = _start("u_persist", title="t", job="J1", category="work", duration="30 min")
    ev = store.get_event(r["id"])
    assert ev["job"] == "J1" and ev["category"] == "work"
    assert r["status"] == "confirmed"  # fixed duration -> immediately confirmed


def test_autoswitch_is_per_user():
    a = _start("u_switch", title="first", job="a")           # open-ended -> active
    assert a["status"] == "active" and "switched_from" not in a
    b = _start("u_switch", title="second", job="b")          # must auto-switch
    assert b.get("switched_from") and len(b["switched_from"]) == 1
    assert "warning" in b
    # exactly one active remains for this owner; a different owner is untouched
    _start("u_other", title="theirs", job="x")
    assert len(store.list_active(owner="u_switch")) == 1
    assert len(store.list_active(owner="u_other")) == 1


def test_summarize_and_list_jobs_with_category_filter():
    o = "u_sum"
    _start(o, title="acme1", job="acme", category="work", duration="2 hours")
    _start(o, title="acme2", job="acme", category="work", duration="30 min")
    _start(o, title="thesis", job="thesis", category="personal", duration="1 hour")

    lo = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    hi = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    summ = store.summarize_jobs(o, lo, hi)
    by_job = {j["job"]: j for j in summ["jobs"]}
    assert by_job["acme"]["total_seconds"] == 2 * 3600 + 30 * 60
    assert by_job["acme"]["count"] == 2
    assert summ["total_seconds"] == 2 * 3600 + 30 * 60 + 3600

    # category filter on list_jobs (the calendar_list_jobs `category` arg)
    work_only = store.list_jobs(o, category="WORK")  # case-insensitive
    assert {j["job"] for j in work_only} == {"acme"}
    handler_filtered = res(cal._handle_calendar_list_jobs(
        {"owner": o, "category": "personal"}))
    assert {j["job"] for j in handler_filtered["jobs"]} == {"thesis"}


def test_resume_reuses_exact_spelling_and_handles_missing():
    o = "u_resume"
    # missing with no jobs -> guidance, not a silent new job
    miss = json.loads(cal._handle_calendar_resume_job({"owner": o, "job": "ghost"}))
    assert not miss["ok"] and "no jobs yet" in miss["error"]

    _start(o, title="Acme work", job="Client-ACME", category="work", duration="20 min")
    r = res(cal._handle_calendar_resume_job({"owner": o, "job": "client-acme"}))
    assert r["resumed"] is True
    assert r["resumed_from"]["job"] == "Client-ACME"      # exact stored spelling
    ev = store.get_event(r["id"])
    assert ev["job"] == "Client-ACME" and ev["category"] == "work"
    res(cal._handle_calendar_stop_timer({"owner": o}))     # don't leave it running

    # unknown name but jobs exist -> ask for the exact name + list them
    typo = json.loads(cal._handle_calendar_resume_job({"owner": o, "job": "nope"}))
    assert not typo["ok"] and "Client-ACME" in typo["error"]


def test_update_event_can_clear_category():
    ev = res(cal._handle_calendar_add_event({
        "title": "meeting", "owner": "u_upd",
        "start": "2026-06-10T14:00:00+03:00", "category": "work",
    }))
    assert store.get_event(ev["id"])["category"] == "work"
    res(cal._handle_calendar_update_event({"id": ev["id"], "category": None}))
    assert store.get_event(ev["id"])["category"] is None


def test_period_window_math():
    def days(period, anchor):
        lo, hi = cal._resolve_period_window(period, anchor, "Indian/Antananarivo")
        return (datetime.fromisoformat(hi) - datetime.fromisoformat(lo)).days
    assert days("daily", "2026-06-03") == 1
    assert days("weekly", "2026-06-03") == 7
    assert days("monthly", "2026-06-03") == 30   # June
    assert days("monthly", "2026-02-15") == 28   # Feb 2026
    assert days("yearly", "2026-06-03") == 365


# ---------------------------------------------------------------------------
# New tests: registry gating, email fallback, categories, timers shared logic
# ---------------------------------------------------------------------------

def test_registry_blocks_unknown_owner():
    """Unregistered owner is refused by add_event, start_timer, resume_job."""
    bad_owner = "ghost_user_xyz"

    # calendar_add_event
    r_add = json.loads(cal._handle_calendar_add_event({
        "title": "test", "owner": bad_owner,
        "start": "2026-06-10T14:00:00+03:00",
    }))
    assert not r_add["ok"]
    assert "calendar-users.json" in r_add["error"]

    # calendar_start_timer
    r_start = json.loads(cal._handle_calendar_start_timer({
        "title": "test timer", "owner": bad_owner,
    }))
    assert not r_start["ok"]
    assert "calendar-users.json" in r_start["error"]

    # calendar_resume_job
    r_resume = json.loads(cal._handle_calendar_resume_job({
        "owner": bad_owner, "job": "some-job",
    }))
    assert not r_resume["ok"]
    assert "calendar-users.json" in r_resume["error"]

    # A registered owner succeeds for add_event (no email needed, just title+start+owner)
    r_ok = json.loads(cal._handle_calendar_add_event({
        "title": "valid event", "owner": "u_reg",
        "start": "2026-06-10T14:00:00+03:00",
    }))
    assert r_ok["ok"], f"Expected success for registered owner, got: {r_ok}"


def test_create_planning_registry_gate():
    """calendar_create_planning refuses unregistered owners; a registered owner
    with no email is rejected with the email-specific message (not the registry
    one); a registered owner with an email succeeds."""
    base = {"name": "Q3 objectives", "period_start": "2026-07-01", "period_end": "2026-10-01"}

    # unregistered -> registry refusal
    r_bad = json.loads(cal._handle_calendar_create_planning({**base, "owner": "ghost_user_xyz"}))
    assert not r_bad["ok"] and "calendar-users.json" in r_bad["error"]

    # registered but no email -> email-specific rejection, NOT the registry message
    r_noemail = json.loads(cal._handle_calendar_create_planning({**base, "owner": "u_noemail"}))
    assert not r_noemail["ok"]
    assert "email" in r_noemail["error"].lower()
    assert "calendar-users.json" not in r_noemail["error"]

    # registered with an email (via the registry fallback) -> succeeds
    r_ok = json.loads(cal._handle_calendar_create_planning({**base, "owner": "u_plan"}))
    assert r_ok["ok"], f"Expected success, got: {r_ok}"


def test_list_events_explicit_range_and_today_default():
    """Regression: explicit from/to must not crash (it passed _parse_start the
    wrong arity), and the default window must include events earlier *today*.

    Computed in the plugin's DEFAULT_TZ so it's robust to a configured CALENDAR_TZ.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cal.recurrence_mod.DEFAULT_TZ)
    local_now = datetime.now(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Midpoint of "today so far": strictly earlier today (so the OLD now-anchored
    # default would have excluded it), regardless of timezone or hour of day.
    event_local = local_midnight + (local_now - local_midnight) / 2
    add = res(cal._handle_calendar_add_event({
        "owner": "u_reg", "title": "Earlier today ev",
        "start": event_local.astimezone(timezone.utc).isoformat(),
        "alert_channel": "none",
    }))
    assert add["created"]

    # explicit from/to spanning today (previously raised TypeError -> tool_error)
    day0 = local_midnight.astimezone(timezone.utc).isoformat()
    day1 = (local_midnight + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    r1 = res(cal._handle_calendar_list_events(
        {"owner": "u_reg", "from": day0, "to": day1, "query": "Earlier"}))
    assert any(e["title"] == "Earlier today ev" for e in r1["events"])

    # default window (no from/to) must still include the earlier-today event
    r2 = res(cal._handle_calendar_list_events({"owner": "u_reg", "query": "Earlier"}))
    assert any(e["title"] == "Earlier today ev" for e in r2["events"])


def test_get_user_email_registry_fallback():
    """An owner with only a registry email (no user_emails row) resolves via store.get_user_email."""
    # "Toavina" is registered with email "t@example.com" in the registry.
    # Ensure there is no user_emails row for them so the fallback path is exercised.
    # (They may have been set previously; we remove any DB row to force the fallback.)
    key = "toavina"
    try:
        store.remove_user_email(key)
    except Exception:
        pass

    email = store.get_user_email("Toavina")
    assert email == "t@example.com", (
        f"Expected registry fallback email 't@example.com', got {email!r}"
    )


def test_list_categories():
    """store.list_categories returns distinct categories for an owner."""
    o = "u_cat"
    _start(o, title="task1", job="j1", category="alpha", duration="10 min")
    _start(o, title="task2", job="j2", category="beta",  duration="10 min")
    _start(o, title="task3", job="j3", category="Alpha", duration="10 min")  # duplicate (case)
    _start(o, title="task4", job="j4", duration="10 min")  # no category — excluded

    cats = store.list_categories(owner=o)
    # Should include alpha and beta (exact stored spellings, case-insensitively sorted).
    # Both "alpha" and "Alpha" exist; SQLite DISTINCT preserves both actual stored values.
    cats_lower = [c.lower() for c in cats]
    assert "alpha" in cats_lower, f"Expected 'alpha' in categories, got {cats}"
    assert "beta" in cats_lower, f"Expected 'beta' in categories, got {cats}"
    # No category entries should appear.
    assert None not in cats


def test_timers_resume_shared_logic():
    """timers.resume_job returns not_found for unknown job, and ok after a session exists."""
    o = "u_resume2"
    # Ensure u_resume2 is in the registry (it's not — use a registered owner instead)
    # Use u_sum which is registered and has sessions
    o = "u_sum"

    # Try resuming a job that was never started for this isolated check.
    res_miss = timers.resume_job(o, "nonexistent_job_zzz")
    assert res_miss["ok"] is False
    assert res_miss["reason"] == "not_found"
    assert isinstance(res_miss["existing_jobs"], list)

    # Start a session so the job exists, then resume it.
    _start(o, title="SharedJobTest", job="shared-job-x", category="testcat", duration="5 min")
    res_ok = timers.resume_job(o, "shared-job-x")
    assert res_ok["ok"] is True
    result = res_ok["result"]
    assert result.get("resumed") is True
    assert result["resumed_from"]["job"] == "shared-job-x"
    assert result["resumed_from"]["category"] == "testcat"
    # Stop the open timer so it doesn't pollute other tests.
    try:
        res(cal._handle_calendar_stop_timer({"owner": o}))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Notes feature (alertless quick-capture entries)
# ---------------------------------------------------------------------------

def test_kind_migration_includes_kind():
    cols = {r[1] for r in store._get_conn().execute("PRAGMA table_info(events)")}
    assert "kind" in cols


def test_add_note_persists_kind_and_is_alertless():
    r = res(cal._handle_calendar_add_note({
        "owner": "u_note", "content": "Call the bank", "tags": ["finance"],
    }))
    assert r["kind"] == "note"
    ev = store.get_event(r["id"])
    assert ev["kind"] == "note"
    assert ev["alert_channel"] == "none"        # alertless
    assert ev["title"] == "Call the bank"
    assert ev["recurrence"] is None


def test_add_note_requires_registered_owner():
    bad = json.loads(cal._handle_calendar_add_note({
        "owner": "ghost_user_xyz", "content": "x"}))
    assert not bad["ok"] and "calendar-users.json" in bad["error"]


def test_list_notes_recent_first_and_filters():
    o = "u_notelist"
    now = datetime.now(timezone.utc)
    res(cal._handle_calendar_add_note({"owner": o, "content": "alpha note",
        "when": (now - timedelta(days=10)).isoformat(), "tags": ["old"]}))
    res(cal._handle_calendar_add_note({"owner": o, "content": "beta idea",
        "when": (now - timedelta(days=2)).isoformat()}))
    res(cal._handle_calendar_add_note({"owner": o, "content": "gamma thought",
        "when": (now - timedelta(hours=1)).isoformat()}))

    alln = res(cal._handle_calendar_list_notes({"owner": o}))
    whens = [n["when_utc"] for n in alln["notes"]]
    assert whens == sorted(whens, reverse=True)          # most-recent-first
    assert alln["count"] == 3

    # text query (substring over content/details/tags)
    q = res(cal._handle_calendar_list_notes({"owner": o, "query": "idea"}))
    assert [n["content"] for n in q["notes"]] == ["beta idea"]

    # date-range over the note timestamp: last 3 days excludes the 10-day-old note
    lo = (now - timedelta(days=3)).isoformat()
    hi = (now + timedelta(days=1)).isoformat()
    rng = res(cal._handle_calendar_list_notes({"owner": o, "from": lo, "to": hi}))
    assert {n["content"] for n in rng["notes"]} == {"beta idea", "gamma thought"}


def test_notes_excluded_from_agenda_and_digest():
    from zoneinfo import ZoneInfo
    o = "u_noteflt"
    tz = ZoneInfo(cal.recurrence_mod.DEFAULT_TZ)
    today9 = datetime.now(tz).replace(hour=9, minute=0, second=0, microsecond=0)
    res(cal._handle_calendar_add_event({"owner": o, "title": "Real event",
        "start": today9.astimezone(timezone.utc).isoformat(), "alert_channel": "none"}))
    res(cal._handle_calendar_add_note({"owner": o, "content": "A note today"}))

    # agenda (calendar_list_events) excludes the note
    titles = {e["title"] for e in res(cal._handle_calendar_list_events({"owner": o}))["events"]}
    assert "Real event" in titles and "A note today" not in titles

    # store-level kind filter
    assert {e["title"] for e in store.list_events(owner=o, kind="note")} == {"A note today"}
    assert "A note today" not in {e["title"] for e in store.list_events(owner=o, kind="event")}

    # daily digest excludes the note
    dtitles = {i["title"] for i in cal.digest_mod.build_owner_digest(o)["today"]}
    assert "Real event" in dtitles and "A note today" not in dtitles


def test_log_job_records_past_session_and_aggregates():
    """calendar_log_job logs a completed past session (source=timer, started/
    ended/duration) that aggregates in summarize_jobs exactly like a stop."""
    o = "u_log"
    start = "2026-06-03T14:00:00+03:00"   # 2pm
    end = "2026-06-03T16:30:00+03:00"     # 4:30pm  -> 2h30m
    r = res(cal._handle_calendar_log_job({
        "owner": o, "job": "client-acme", "category": "work",
        "start": start, "end": end, "title": "ACME retro",
    }))
    assert r["logged"] is True and r["status"] == "confirmed"
    assert r["duration_seconds"] == 2 * 3600 + 30 * 60

    # The occurrence_status carries the timer fields.
    st = store.list_statuses(r["id"])[0]
    assert st["status"] == "confirmed" and st["source"] == "timer"
    assert st["started_utc"] and st["ended_utc"] and st["duration_seconds"] == 9000
    ev = store.get_event(r["id"])
    assert ev["job"] == "client-acme" and ev["category"] == "work"
    assert ev["alert_channel"] == "none"   # alertless

    # It aggregates in the job summary over a window covering that past day.
    summ = store.summarize_jobs(o, "2026-06-01T00:00:00+00:00", "2026-06-30T00:00:00+00:00")
    by_job = {j["job"]: j for j in summ["jobs"]}
    assert by_job["client-acme"]["total_seconds"] == 9000
    assert by_job["client-acme"]["count"] == 1


def test_log_job_rejects_future_and_bad_range():
    o = "u_log"
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    future_start = (_dt.now(_tz.utc) + _td(days=1)).isoformat()
    future_end = (_dt.now(_tz.utc) + _td(days=1, hours=2)).isoformat()
    bad_future = json.loads(cal._handle_calendar_log_job({
        "owner": o, "job": "x", "start": future_start, "end": future_end}))
    assert not bad_future["ok"] and "future" in bad_future["error"]

    bad_range = json.loads(cal._handle_calendar_log_job({
        "owner": o, "job": "x",
        "start": "2026-06-03T16:00:00+03:00", "end": "2026-06-03T14:00:00+03:00"}))
    assert not bad_range["ok"] and "after start" in bad_range["error"]

    # duration form works (no explicit end)
    ok = res(cal._handle_calendar_log_job({
        "owner": o, "job": "thesis", "start": "2026-06-02T09:00:00+03:00", "duration": "45 min"}))
    assert ok["duration_seconds"] == 45 * 60

    # unregistered owner refused
    bad_owner = json.loads(cal._handle_calendar_log_job({
        "owner": "ghost_user_xyz", "job": "x", "start": "2026-06-03T14:00:00+03:00", "duration": "1h"}))
    assert not bad_owner["ok"] and "calendar-users.json" in bad_owner["error"]


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} tests passed")


if __name__ == "__main__":
    _run_all()
