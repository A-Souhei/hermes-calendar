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
from . import planning as planning_mod
from . import recurrence
from . import store

logger = logging.getLogger(__name__)

_running = False
_thread: threading.Thread | None = None
_lock = threading.Lock()


_VALID_LANGUAGES = ("en", "fr")


def _load_config() -> dict:
    env_lang = os.environ.get("CALENDAR_DEFAULT_LANG", "").strip().lower()
    default_language = env_lang if env_lang in _VALID_LANGUAGES else "en"
    defaults = {
        "default_lead_seconds": 3600,
        "daily_alert_hour": 9,
        "check_interval_seconds": 60,
        "boot_catchup_seconds": 7200,
        "default_language": default_language,
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
    # Validate the language value that may have come from the JSON file.
    if defaults.get("default_language") not in _VALID_LANGUAGES:
        defaults["default_language"] = "en"
    return defaults


_EN_WEEKDAYS = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]
_FR_WEEKDAYS = [
    "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche",
]
_EN_MONTHS = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_FR_MONTHS = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def _resolve_lang(event: dict) -> str:
    """Effective reminder language: event-level → config default → 'en'."""
    lang = (event.get("language") or _load_config().get("default_language") or "en")
    return lang if lang in _VALID_LANGUAGES else "en"


def _chat_footer(lang: str) -> str:
    """Localized management hint appended to the CHAT (Telegram) reminder only —
    never to the phone push / TTS (so the voice doesn't read it)."""
    if lang == "fr":
        return "— Pour modifier ou annuler cet événement, écris-moi (ex. « annule cet événement »)."
    return "— To change or cancel this event, just message me (e.g. \"cancel this event\")."


def _build_message(event: dict, occ_utc: datetime) -> str:
    """Compose the reminder notification body, localized to the event's language."""
    from zoneinfo import ZoneInfo

    # Resolve effective language: event-level → config default → 'en'
    lang = _resolve_lang(event)

    tz_name = event.get("tz") or recurrence.DEFAULT_TZ
    try:
        event_tz = ZoneInfo(tz_name)
    except Exception:
        event_tz = ZoneInfo(recurrence.DEFAULT_TZ)

    occ_local = occ_utc.astimezone(event_tz)
    wd = occ_local.weekday()   # 0=Mon … 6=Sun
    dd = f"{occ_local.day:02d}"
    mm = occ_local.month
    yyyy = occ_local.year
    hhmm = f"{occ_local.hour:02d}:{occ_local.minute:02d}"
    tz_abbr = occ_local.strftime("%Z")

    lines: list[str] = []

    if lang == "fr":
        weekday_name = _FR_WEEKDAYS[wd]
        month_name = _FR_MONTHS[mm]
        if event.get("all_day"):
            time_str = f"{weekday_name} {dd} {month_name} {yyyy}"
        else:
            time_str = f"{weekday_name} {dd} {month_name} {yyyy} à {hhmm} {tz_abbr}"
        lines.append(f"⏰ Rappel : {event['title']}")
        lines.append(time_str)
        if event.get("description"):
            lines.append(event["description"])
        meeting = event.get("meeting")
        if isinstance(meeting, dict) and meeting.get("room_url"):
            app = meeting.get("room_app", "")
            room_url = meeting["room_url"]
            if app:
                lines.append(f"Rejoindre via {app} : {room_url}")
            else:
                lines.append(f"Rejoindre : {room_url}")
        if event.get("location"):
            lines.append(f"Lieu : {event['location']}")
    else:
        weekday_name = _EN_WEEKDAYS[wd]
        month_name = _EN_MONTHS[mm]
        if event.get("all_day"):
            time_str = f"{weekday_name}, {month_name} {dd} {yyyy}"
        else:
            time_str = f"{weekday_name}, {month_name} {dd} {yyyy} at {hhmm} {tz_abbr}"
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


def _process_due(since_utc, now_utc, default_lead, daily_hour):
    """Fire any due, unfired alerts in (since_utc, now_utc].

    Returns (fired_count, chat_messages). HA channels (ha_notify / ha_speak)
    are sent immediately via notify.fire(); the "chat" channel can't be sent
    from here (no chat runtime) so its message is returned for the cron tick
    to print to stdout, which the --no-agent cron posts into the chat.
    """
    fired = 0
    chat_msgs: list[str] = []
    for ev in store.list_events():
        try:
            for occ_iso, _alert_utc in recurrence.due_alerts(
                ev, since_utc, now_utc, default_lead, daily_hour
            ):
                if store.was_fired(ev["id"], occ_iso):
                    continue
                try:
                    occ_utc = datetime.fromisoformat(occ_iso)
                    msg = _build_message(ev, occ_utc)
                    for channel in notify.resolve_channels(ev.get("alert_channel")):
                        if channel == "chat":
                            chat_msgs.append(msg + "\n\n" + _chat_footer(_resolve_lang(ev)))
                            fired += 1
                            logger.info("calendar: queued chat reminder event=%s occ=%s",
                                        ev["id"], occ_iso)
                            continue
                        if channel == "email":
                            recipient = ev.get("notify_email") or (
                                store.get_user_email(ev["owner"]) if ev.get("owner") else None
                            )
                            if not recipient:
                                logger.info(
                                    "calendar: email channel but no recipient (owner=%s) — skipping",
                                    ev.get("owner"))
                                continue
                            allowed = notify.allowed_email_recipients()
                            if recipient.lower() not in allowed:
                                logger.warning(
                                    "calendar: refusing email to non-allowlisted recipient %s",
                                    recipient)
                                continue
                            result = notify.fire("email", ev["title"], msg, target=recipient)
                            if result.get("ok"):
                                fired += 1
                                logger.info("calendar: emailed reminder event=%s occ=%s to=%s",
                                            ev["id"], occ_iso, recipient)
                            else:
                                logger.warning("calendar: email failed event=%s occ=%s: %s",
                                               ev["id"], occ_iso, result.get("error"))
                            continue
                        result = notify.fire(channel, ev["title"], msg)
                        if result.get("ok"):
                            fired += 1
                            logger.info("calendar: fired alert event=%s occ=%s channel=%s",
                                        ev["id"], occ_iso, channel)
                        else:
                            logger.warning("calendar: notify failed event=%s occ=%s channel=%s: %s",
                                           ev["id"], occ_iso, channel, result.get("error"))
                except Exception as fire_exc:
                    logger.exception("calendar: error firing alert event=%s occ=%s: %s",
                                     ev["id"], occ_iso, fire_exc)
                store.mark_fired(ev["id"], occ_iso)  # mark even on failure (no retry storm)
        except Exception as ev_exc:
            logger.exception("calendar: error processing event=%s: %s", ev.get("id"), ev_exc)
    return fired, chat_msgs


def _process_due_planning_reports(now_utc: datetime) -> list[str]:
    """Auto-email end-of-period reports for plannings whose report is due.

    For each planning with report_sent==0 whose report_due (09:00 in DEFAULT_TZ
    on the calendar date of period_end_utc, i.e. the morning after the period)
    has passed, build the localized report, email it to the owner if a known &
    allowlisted address exists, mark it sent (so it fires once), and return a
    localized one-liner per planning for the cron to post into chat.

    Robust per-planning: one bad planning never breaks the tick.
    """
    notices: list[str] = []
    try:
        plannings = store.list_plannings()
    except Exception as exc:
        logger.exception("calendar: failed to list plannings for reports: %s", exc)
        return notices

    for p in plannings:
        try:
            if p.get("report_sent"):
                continue
            due = planning_mod.report_due_utc(p)
            if now_utc < due:
                continue

            stats = planning_mod.planning_stats(p)
            owner = p.get("owner")
            owner_email = store.get_user_email(owner) if owner else None

            emailed = False
            if owner_email and owner_email.lower() in notify.allowed_email_recipients():
                lang = planning_mod._planning_lang(p)
                pdf = planning_mod.render_report_pdf(stats, lang)
                attachments = None
                if pdf:
                    import re
                    raw = p.get("name") or "planning"
                    safe = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower() or "planning"
                    attachments = [(f"planning-report-{safe}.pdf", pdf, "pdf")]
                result = notify.fire(
                    "email",
                    planning_mod.report_subject(p),
                    stats["text"],
                    target=owner_email,
                    attachments=attachments,
                )
                emailed = bool(result.get("ok"))
                if not emailed:
                    logger.warning(
                        "calendar: planning report email failed planning=%s: %s",
                        p.get("id"), result.get("error"))

            lang = planning_mod._planning_lang(p)
            name = p.get("name") or ""
            if emailed:
                if lang == "fr":
                    notices.append(
                        f"📋 Le rapport de ton planning « {name} » est prêt "
                        "— envoyé par email.")
                else:
                    notices.append(
                        f"📋 Your '{name}' planning report is ready — emailed to you.")
            else:
                if lang == "fr":
                    notices.append(
                        f"📋 Le rapport de ton planning « {name} » est prêt "
                        "— aucune adresse email enregistrée.")
                else:
                    notices.append(
                        f"📋 Your '{name}' planning report is ready "
                        "— no email address on file.")

            store.set_report_sent(p["id"])
        except Exception as exc:
            logger.exception("calendar: error processing planning report %s: %s",
                             p.get("id"), exc)
    return notices


def _state_path() -> str:
    return os.path.join(
        os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")), "calendar_last_tick"
    )


def tick_once() -> int:
    """One alert pass — for an every-minute `hermes cron --no-agent` job.

    Looks back to the last recorded tick (capped at max_catchup_seconds) so a
    brief gateway/cron downtime still catches up; fired_alerts dedup prevents
    repeats. Reliable regardless of agent activity.
    """
    cfg = _load_config()
    now = datetime.now(timezone.utc)
    max_catch = int(cfg.get("max_catchup_seconds", 21600))   # 6h cap
    lookback = int(cfg.get("lookback_seconds", 180))
    last = None
    sp = _state_path()
    try:
        if os.path.exists(sp):
            last = datetime.fromisoformat(open(sp).read().strip())
    except Exception:
        last = None
    since = (now - timedelta(seconds=lookback)) if last is None \
        else max(last, now - timedelta(seconds=max_catch))
    fired, chat_msgs = _process_due(
        since, now, int(cfg["default_lead_seconds"]), int(cfg["daily_alert_hour"])
    )
    # End-of-period planning reports (auto-emailed once at 09:00 the morning
    # after the period; deduped via report_sent). Returns localized chat notices.
    planning_notices = _process_due_planning_reports(now)
    try:
        with open(sp, "w") as f:
            f.write(now.isoformat())
    except Exception:
        logger.warning("calendar: could not write last-tick state")
    # Print any "chat"-channel reminders AND planning report notices to stdout —
    # the --no-agent cron that runs this tick delivers stdout straight into the
    # chat. Nothing printed when there's nothing to say, so the cron stays silent.
    out_lines = list(chat_msgs) + list(planning_notices)
    if out_lines:
        print("\n\n".join(out_lines))
    return fired


def _loop() -> None:
    cfg = _load_config()
    default_lead = int(cfg["default_lead_seconds"])
    daily_hour = int(cfg["daily_alert_hour"])
    check_interval = int(cfg["check_interval_seconds"])
    catchup = int(cfg["boot_catchup_seconds"])
    last_check = datetime.now(timezone.utc) - timedelta(seconds=catchup)
    logger.info("calendar scheduler thread started; catchup window %ds", catchup)
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Chat reminders are intentionally dropped on this path — the loop
            # has no chat runtime; "chat" is delivered only by the cron tick.
            _process_due(last_check, now, default_lead, daily_hour)
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
