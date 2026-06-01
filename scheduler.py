"""Background alert loop for the calendar plugin.

Runs as a daemon thread; idempotent start(). Fires due reminders via notify.py,
deduplicating through store.fired_alerts. A boot_catchup_seconds window ensures
brief downtime does not cause missed alerts (fired_alerts prevents re-fire).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from . import notify
from . import recurrence
from . import store

logger = logging.getLogger(__name__)

_running = False
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _load_config() -> dict:
    defaults = {
        "default_lead_seconds": 3600,
        "daily_alert_hour": 9,
        "check_interval_seconds": 60,
        "boot_catchup_seconds": 7200,
    }
    cfg_path = os.path.join(
        os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
        "calendar_config.json",
    )
    try:
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                file_cfg = json.load(f)
            defaults.update({k: v for k, v in file_cfg.items() if v is not None})
    except Exception:
        pass
    return defaults


def _build_message(event: dict, occ_utc: datetime) -> str:
    """Compose the reminder notification body."""
    from zoneinfo import ZoneInfo

    tz_name = event.get("tz") or recurrence.DEFAULT_TZ
    try:
        event_tz = ZoneInfo(tz_name)
    except Exception:
        event_tz = ZoneInfo(recurrence.DEFAULT_TZ)

    occ_local = occ_utc.astimezone(event_tz)
    lines: list[str] = []

    if event.get("all_day"):
        time_str = occ_local.strftime("%A, %B %d %Y")
    else:
        time_str = occ_local.strftime("%A, %B %d %Y at %H:%M %Z")

    lines.append(f"⏰ Reminder: {event['title']}")
    lines.append(time_str)

    if event.get("description"):
        lines.append(event["description"])

    meeting = event.get("meeting")
    if isinstance(meeting, dict) and meeting.get("room_url"):
        app = meeting.get("room_app", "")
        room_url = meeting["room_url"]
        if app:
            lines.append(f"Join via {app}: {room_url}")
        else:
            lines.append(f"Join: {room_url}")

    if event.get("location"):
        lines.append(f"Location: {event['location']}")

    return "\n".join(lines)


def _loop() -> None:
    global _running

    cfg = _load_config()
    default_lead = int(cfg["default_lead_seconds"])
    daily_hour = int(cfg["daily_alert_hour"])
    check_interval = int(cfg["check_interval_seconds"])
    catchup = int(cfg["boot_catchup_seconds"])

    last_check = datetime.now(timezone.utc) - timedelta(seconds=catchup)
    logger.info("calendar scheduler started; catchup window %ds", catchup)

    while True:
        try:
            now = datetime.now(timezone.utc)
            events = store.list_events()

            for ev in events:
                try:
                    alerts = recurrence.due_alerts(
                        ev, last_check, now, default_lead, daily_hour
                    )
                    for occ_iso, _alert_utc in alerts:
                        if store.was_fired(ev["id"], occ_iso):
                            continue
                        try:
                            occ_utc = datetime.fromisoformat(occ_iso)
                            msg = _build_message(ev, occ_utc)
                            channel = ev.get("alert_channel") or "ha_notify"
                            result = notify.fire(channel, ev["title"], msg)
                            if result["ok"]:
                                logger.info(
                                    "calendar: fired alert event=%s occ=%s channel=%s",
                                    ev["id"], occ_iso, channel,
                                )
                            else:
                                logger.warning(
                                    "calendar: notify failed event=%s occ=%s: %s",
                                    ev["id"], occ_iso, result.get("error"),
                                )
                        except Exception as fire_exc:
                            logger.exception(
                                "calendar: error firing alert event=%s occ=%s: %s",
                                ev["id"], occ_iso, fire_exc,
                            )
                        # Always mark fired to prevent infinite retries on broken events
                        store.mark_fired(ev["id"], occ_iso)
                except Exception as ev_exc:
                    logger.exception(
                        "calendar: error processing event=%s: %s", ev.get("id"), ev_exc
                    )

            last_check = now

        except Exception as tick_exc:
            logger.exception("calendar scheduler tick error: %s", tick_exc)

        time.sleep(check_interval)


def start() -> None:
    """Start the background scheduler thread. Idempotent."""
    global _running, _thread
    with _lock:
        if _running:
            return
        _running = True
        _thread = threading.Thread(target=_loop, name="calendar-scheduler", daemon=True)
        _thread.start()
        logger.info("calendar scheduler thread launched")
