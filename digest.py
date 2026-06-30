"""Daily digest builder for the calendar plugin.

Produces a per-owner digest of today's events (or the single closest upcoming
event when today is empty), rendered as markdown and as a styled HTML email.
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import recurrence
from . import store


# ---------------------------------------------------------------------------
# Core digest builder
# ---------------------------------------------------------------------------

def build_owner_digest(
    owner: str,
    *,
    now_utc: Optional[datetime] = None,
    tz_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a digest dict for *owner*.

    Returns::

        {
            "owner":           str,
            "tz":              str,
            "date_str":        str,   # e.g. "Friday, 5 June 2026"
            "today":           list,  # items with today's occurrences
            "next_up":         dict|None,
            "has_events_today": bool,
        }

    Each item in ``today`` has: occurrence_utc, local_time, title, all_day,
    location, recurring, status (effective).

    ``next_up`` (used when today is empty) has: occurrence_utc, local_dt_str,
    weekday, title, all_day, location.
    """
    tz = tz_name or recurrence.DEFAULT_TZ
    now = now_utc or datetime.now(timezone.utc)

    try:
        event_tz = ZoneInfo(tz)
    except Exception:
        event_tz = ZoneInfo(recurrence.DEFAULT_TZ)
        tz = recurrence.DEFAULT_TZ

    # Today's window: local midnight today → local midnight tomorrow, in UTC.
    now_local = now.astimezone(event_tz)
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_local.astimezone(timezone.utc)
    day_end_utc = (day_start_local + timedelta(days=1)).astimezone(timezone.utc)

    # Build the date label without platform-specific strftime extensions
    # (e.g. %-d is unsupported on Windows) — interpolate the numeric day.
    date_str = (
        f"{day_start_local.strftime('%A')}, {day_start_local.day} "
        f"{day_start_local.strftime('%B %Y')}"
    )

    events = store.list_events(owner=owner, kind="event")

    today_items: List[Dict[str, Any]] = []

    for ev in events:
        ev_copy = dict(ev)
        try:
            ev_copy["_exceptions"] = store.get_exceptions(ev["id"])
        except Exception:
            ev_copy["_exceptions"] = set()

        try:
            occs = recurrence.occurrences(ev_copy, day_start_utc, day_end_utc)
        except Exception:
            occs = []

        ev_tz_name = ev.get("tz") or tz
        try:
            ev_tz = ZoneInfo(ev_tz_name)
        except Exception:
            ev_tz = event_tz

        is_recurring = bool(ev.get("recurrence"))

        for occ_utc in occs:
            occ_iso = occ_utc.isoformat()
            occ_local = occ_utc.astimezone(ev_tz)

            status_row = store.get_status(ev["id"], occ_iso)
            stored_status = status_row["status"] if status_row else "floating"
            effective = _effective_status(stored_status, occ_utc, now, is_job=bool(ev.get("job")))

            today_items.append({
                "occurrence_utc": occ_iso,
                "local_time": occ_local.strftime("%H:%M") if not ev.get("all_day") else None,
                "title": ev["title"],
                "all_day": bool(ev.get("all_day")),
                "location": ev.get("location"),
                "recurring": is_recurring,
                "status": effective,
            })

    today_items.sort(key=lambda x: x["occurrence_utc"])

    next_up: Optional[Dict[str, Any]] = None
    if not today_items:
        # Find the single closest future occurrence across all the owner's events.
        best_utc: Optional[datetime] = None
        best_item: Optional[Dict[str, Any]] = None

        for ev in events:
            try:
                nxt = recurrence.next_occurrence(ev, now)
            except Exception:
                nxt = None
            if nxt is None:
                continue
            if best_utc is None or nxt < best_utc:
                best_utc = nxt
                ev_tz_name = ev.get("tz") or tz
                try:
                    ev_tz = ZoneInfo(ev_tz_name)
                except Exception:
                    ev_tz = event_tz
                nxt_local = nxt.astimezone(ev_tz)
                _when = "All day" if ev.get("all_day") else nxt_local.strftime("%H:%M")
                best_item = {
                    "occurrence_utc": nxt.isoformat(),
                    "local_dt_str": f"{nxt_local.day} {nxt_local.strftime('%b')} · {_when}",
                    "weekday": nxt_local.strftime("%A"),
                    "title": ev["title"],
                    "all_day": bool(ev.get("all_day")),
                    "location": ev.get("location"),
                }

        next_up = best_item

    return {
        "owner": owner,
        "tz": tz,
        "date_str": date_str,
        "today": today_items,
        "next_up": next_up,
        "has_events_today": bool(today_items),
    }


def _effective_status(stored: str, occ: datetime, now: datetime, is_job: bool = False) -> str:
    """Mirror of dashboard/plugin_api._effective_status.

    A floating past occurrence reads as 'missed' (unconfirmed); a floating
    future one stays 'floating'. Non-floating statuses pass through unchanged.
    A job/timer session is never 'missed' — it is tracked (confirmed/active) or
    simply untracked, so a floating job stays 'floating'.
    """
    if stored != "floating":
        return stored
    if is_job:
        return "floating"
    return "missed" if occ < now else "floating"


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def render_markdown(digest: Dict[str, Any]) -> str:
    """Render a digest dict as markdown, matching the github-daily shape.

    Produces ``### Section`` headers and ``- `` bullets so that
    ``_markdown_to_html`` converts it correctly.
    """
    lines: List[str] = []
    date_str = digest["date_str"]

    lines.append(f"### Today — {date_str}")

    if digest["has_events_today"]:
        for item in digest["today"]:
            time_part = "All day" if item["all_day"] else (item["local_time"] or "")
            status = item["status"]
            status_label = ""
            if status != "floating":
                status_label = f" — {status}"
            loc = f" @ {item['location']}" if item.get("location") else ""
            lines.append(f"- {time_part} · {item['title']}{status_label}{loc}")
    else:
        lines.append("- No events today.")
        lines.append("")
        lines.append("### Next up")
        nu = digest.get("next_up")
        if nu:
            time_part = nu["local_dt_str"]
            loc = f" @ {nu['location']}" if nu.get("location") else ""
            lines.append(f"- {nu['weekday']} {time_part} · {nu['title']}{loc}")
        else:
            lines.append("- No upcoming events.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML rendering (replicates the github-daily digest helpers, calendar-branded)
# ---------------------------------------------------------------------------

def _markdown_to_html(digest: str) -> str:
    """Convert markdown (### headers, - bullets, plain lines) to inline-CSS HTML.

    html-escapes all dynamic text; only http(s) URLs become <a> links.
    """
    out: List[str] = []
    in_list = False

    def _close() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in digest.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("### "):
            _close()
            out.append(
                '<h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.04em;'
                'color:#374151;border-bottom:1px solid #e5e7eb;padding-bottom:5px;'
                'margin:20px 0 8px;">'
                + _html.escape(line[4:])
                + "</h2>"
            )
        elif line.startswith("- "):
            if not in_list:
                out.append('<ul style="margin:0 0 6px;padding-left:18px;">')
                in_list = True
            item = line[2:]
            m = re.match(r"\[(.*?)\]\((.*?)\)(.*)$", item)
            if m:
                text, url, rest = m.group(1), m.group(2), m.group(3)
                rest_html = _html.escape(rest)
                if url.startswith("http://") or url.startswith("https://"):
                    out.append(
                        '<li style="margin:4px 0;"><a href="'
                        + _html.escape(url, quote=True)
                        + '" style="color:#2563eb;text-decoration:none;">'
                        + _html.escape(text)
                        + "</a>"
                        + rest_html
                        + "</li>"
                    )
                else:
                    out.append(
                        '<li style="margin:4px 0;">'
                        + _html.escape(text)
                        + rest_html
                        + "</li>"
                    )
            else:
                out.append(
                    '<li style="margin:4px 0;">' + _html.escape(item) + "</li>"
                )
        else:
            _close()
            out.append('<p style="margin:6px 0;">' + _html.escape(line) + "</p>")

    _close()
    return "\n".join(out)


def _html_email(body_html: str, date_str: str) -> str:
    """Wrap body_html in a full HTML email document (gray bg, white card, gradient header)."""
    return (
        "<!doctype html><html><body "
        'style="margin:0;background:#f3f4f6;padding:24px;'
        "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        'color:#1f2933;">'
        '<div style="max-width:640px;margin:0 auto;background:#ffffff;'
        'border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;">'
        '<div style="background:linear-gradient(135deg,#1f2937,#111827);padding:18px 22px;">'
        '<div style="color:#ffffff;font-size:18px;font-weight:700;">\U0001f4c5 Calendar digest</div>'
        '<div style="color:#9ca3af;font-size:12px;margin-top:2px;">'
        + _html.escape(date_str)
        + "</div></div>"
        '<div style="padding:18px 22px;font-size:14px;line-height:1.5;">'
        + body_html
        + "</div>"
        '<div style="padding:12px 22px;border-top:1px solid #e5e7eb;'
        'color:#9ca3af;font-size:11px;">Hermes · calendar-daily</div>'
        "</div></body></html>"
    )


def render_html(digest: Dict[str, Any]) -> str:
    """Render a digest dict as a full HTML email document."""
    return _html_email(_markdown_to_html(render_markdown(digest)), digest["date_str"])
