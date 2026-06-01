#!/usr/bin/env python3
"""Calendar alert tick — for an every-minute `hermes cron --no-agent` job.

Fires any due calendar reminders via Home Assistant (ha_notify / ha_speak) and
prints NOTHING (so the cron delivers nothing to chat — alerts go to the phone).
Runs independently of agent activity, so reminders are reliable.

Wire it up (script lives in ~/.hermes/scripts/):
    hermes cron create "* * * * *" --name calendar-alerts --no-agent \
        --script calendar_tick.py

It loads the installed calendar plugin as a package (default
~/.hermes/plugins/calendar, override with CALENDAR_PLUGIN_DIR) and calls
scheduler.tick_once().
"""

from __future__ import annotations

import importlib.util
import os
import sys


def _plugin_dir() -> str:
    return os.environ.get("CALENDAR_PLUGIN_DIR") or os.path.join(
        os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
        "plugins",
        "calendar",
    )


def main() -> int:
    d = _plugin_dir()
    init_py = os.path.join(d, "__init__.py")
    if not os.path.exists(init_py):
        print(f"calendar_tick: plugin not found at {d}", file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location(
        "calplugin", init_py, submodule_search_locations=[d]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["calplugin"] = mod
    try:
        spec.loader.exec_module(mod)
        mod.scheduler.tick_once()
    except Exception as exc:  # noqa: BLE001
        print(f"calendar_tick error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
