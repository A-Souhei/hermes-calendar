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

# --- isolate storage to a throwaway DB BEFORE the plugin imports store -------
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="caltest_")

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


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} tests passed")


if __name__ == "__main__":
    _run_all()
