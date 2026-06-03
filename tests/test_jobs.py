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


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} tests passed")


if __name__ == "__main__":
    _run_all()
