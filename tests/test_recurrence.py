"""Self-contained tests for nth-weekday-of-month recurrence (bysetpos).

Runs without the Hermes host: it stubs the ``tools.registry`` module that the
plugin imports and loads the plugin package under a synthetic name against a
throwaway DB (``HERMES_HOME`` points at a temp dir). Run directly:

    python tests/test_recurrence.py

or under pytest (the ``test_*`` functions are discovered automatically):

    pytest tests/test_recurrence.py

Requires python-dateutil (a plugin runtime dependency).
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# --- Registry: set up CALENDAR_USERS_FILE FIRST so the plugin sees it -------
# ---------------------------------------------------------------------------

_tmpdir = tempfile.mkdtemp(prefix="caltest_recurrence_")

os.environ["HERMES_HOME"] = _tmpdir

_registry_path = os.path.join(_tmpdir, "calendar-users.json")
_registry_data = {"users": [{"name": "Toavina", "email": "t@example.com", "language": "en"}]}
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
_PKG = "calendar_plugin_under_test_recurrence"


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
recurrence_mod = cal.recurrence_mod


# ---------------------------------------------------------------------------
# _parse_recurrence
# ---------------------------------------------------------------------------

def test_parse_dict_with_valid_bysetpos():
    rec = cal._parse_recurrence({"freq": "monthly", "byweekday": [0], "bysetpos": 2})
    assert rec["bysetpos"] == 2
    assert rec["byweekday"] == [0]
    assert rec["freq"] == "monthly"


def test_parse_dict_with_last_bysetpos():
    rec = cal._parse_recurrence({"freq": "monthly", "byweekday": [4], "bysetpos": -1})
    assert rec["bysetpos"] == -1


def test_parse_dict_drops_invalid_or_zero_bysetpos():
    rec_zero = cal._parse_recurrence({"freq": "monthly", "byweekday": [0], "bysetpos": 0})
    assert "bysetpos" not in rec_zero

    rec_bad = cal._parse_recurrence({"freq": "monthly", "byweekday": [0], "bysetpos": 6})
    assert "bysetpos" not in rec_bad

    rec_nonint = cal._parse_recurrence({"freq": "monthly", "byweekday": [0], "bysetpos": "nope"})
    assert "bysetpos" not in rec_nonint


def test_parse_string_colon_form_2mon():
    rec = cal._parse_recurrence("monthly:2mon")
    assert rec["freq"] == "monthly"
    assert rec["byweekday"] == [0]
    assert rec["bysetpos"] == 2


def test_parse_string_natural_second_monday():
    rec = cal._parse_recurrence("second monday")
    assert rec["freq"] == "monthly"
    assert rec["byweekday"] == [0]
    assert rec["bysetpos"] == 2


def test_parse_string_natural_last_friday():
    rec = cal._parse_recurrence("last friday")
    assert rec["freq"] == "monthly"
    assert rec["byweekday"] == [4]
    assert rec["bysetpos"] == -1


def test_parse_string_natural_of_the_month_suffix():
    rec = cal._parse_recurrence("2nd monday of the month")
    assert rec["freq"] == "monthly"
    assert rec["bysetpos"] == 2

    rec2 = cal._parse_recurrence("last friday of every month")
    assert rec2["bysetpos"] == -1


def test_parse_plain_weekly_unchanged():
    """Existing string behavior must be unaffected — no bysetpos key at all."""
    rec = cal._parse_recurrence("weekly:mon,wed")
    assert rec["freq"] == "weekly"
    assert rec["byweekday"] == [0, 2]
    assert "bysetpos" not in rec


# ---------------------------------------------------------------------------
# recurrence.occurrences()
# ---------------------------------------------------------------------------

def _second_mondays(year_month_pairs):
    """Compute the actual 2nd Monday for each (year, month) via calendar math,
    independent of the code under test, as an oracle."""
    import calendar as _calendar
    out = []
    for year, month in year_month_pairs:
        mondays = [d for d in range(1, _calendar.monthrange(year, month)[1] + 1)
                   if datetime(year, month, d).weekday() == 0]
        out.append(mondays[1])  # 2nd Monday (0-indexed: [0]=1st, [1]=2nd)
    return out


def test_occurrences_second_monday_across_months():
    event = {
        "id": "ev1",
        "start_utc": "2026-01-05T09:00:00+00:00",  # a Monday, anchor only
        "tz": "UTC",
        "recurrence": {"freq": "monthly", "byweekday": [0], "bysetpos": 2},
    }
    range_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    range_end = datetime(2026, 5, 1, tzinfo=timezone.utc)
    occs = recurrence_mod.occurrences(event, range_start, range_end)

    assert len(occs) == 4  # Jan, Feb, Mar, Apr 2026
    expected_days = _second_mondays([(2026, 1), (2026, 2), (2026, 3), (2026, 4)])
    for occ, expected_day in zip(occs, expected_days):
        assert occ.weekday() == 0, f"{occ} is not a Monday"
        assert occ.day == expected_day, f"{occ} is not the 2nd Monday (expected day {expected_day})"


def test_occurrences_last_friday():
    import calendar as _calendar
    event = {
        "id": "ev2",
        "start_utc": "2026-01-02T09:00:00+00:00",  # a Friday, anchor only
        "tz": "UTC",
        "recurrence": {"freq": "monthly", "byweekday": [4], "bysetpos": -1},
    }
    range_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    range_end = datetime(2026, 4, 1, tzinfo=timezone.utc)
    occs = recurrence_mod.occurrences(event, range_start, range_end)

    assert len(occs) == 3  # Jan, Feb, Mar 2026
    for occ in occs:
        assert occ.weekday() == 4, f"{occ} is not a Friday"
        last_day_of_month = _calendar.monthrange(occ.year, occ.month)[1]
        assert last_day_of_month - occ.day < 7, f"{occ} is not in the last week of its month"


def test_occurrences_bysetpos_without_weekday_does_not_vanish():
    """A misconfigured bysetpos (no byweekday, or non-monthly freq) must fall
    back to a sensible non-empty series, never silently produce zero occurrences."""
    no_weekday = {
        "id": "ev3",
        "start_utc": "2026-01-08T09:00:00+00:00",
        "tz": "UTC",
        "recurrence": {"freq": "monthly", "bysetpos": 2},  # no byweekday
    }
    weekly = {
        "id": "ev4",
        "start_utc": "2026-01-05T09:00:00+00:00",  # Monday
        "tz": "UTC",
        "recurrence": {"freq": "weekly", "byweekday": [0], "bysetpos": 2},
    }
    range_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    range_end = datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert len(recurrence_mod.occurrences(no_weekday, range_start, range_end)) == 3  # the 8th, Jan-Mar
    assert len(recurrence_mod.occurrences(weekly, range_start, range_end)) > 3  # every Monday, not empty


# ---------------------------------------------------------------------------
# _human_recurrence
# ---------------------------------------------------------------------------

def test_human_recurrence_second_monday():
    label = cal._human_recurrence({"freq": "monthly", "interval": 1, "byweekday": [0], "bysetpos": 2})
    assert label == "Monthly on the 2nd Monday"


def test_human_recurrence_last_friday():
    label = cal._human_recurrence({"freq": "monthly", "interval": 1, "byweekday": [4], "bysetpos": -1})
    assert label == "Monthly on the last Friday"


def test_human_recurrence_unchanged_without_bysetpos():
    """No bysetpos -> output identical to the pre-existing byweekday rendering."""
    label = cal._human_recurrence({"freq": "weekly", "interval": 1, "byweekday": [0, 2]})
    assert label == "Weekly on Mon, Wed"


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} tests passed")


if __name__ == "__main__":
    _run_all()
