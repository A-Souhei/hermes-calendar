"""Planning statistics + localized report rendering.

A PLANNING is a named, period-bounded set of events (tagged with planning_id).
Completion is scored per occurrence from the existing occurrence_status:
  SUCCESS  = status == "confirmed"
  FAILURE  = everything else (floating/unconfirmed, missed, active)

This module is deliberately dependency-light — it imports only ``store`` and
``recurrence`` (relative) so it can be reused from the plugin's tools, from the
scheduler's cron tick, AND from the read-only dashboard plugin_api (which never
imports ``notify`` / ``tools.registry``).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from . import recurrence
from . import store

_VALID_LANGUAGES = ("en", "fr")


def _default_language() -> str:
    """Configured default language: CALENDAR_DEFAULT_LANG env → 'en'.

    Mirrors scheduler._load_config()'s default (the JSON config file's
    default_language is honoured there; here we keep to the env/'en' fallback
    so this module stays free of scheduler imports)."""
    env_lang = os.environ.get("CALENDAR_DEFAULT_LANG", "").strip().lower()
    return env_lang if env_lang in _VALID_LANGUAGES else "en"


def _planning_lang(planning: Dict[str, Any]) -> str:
    lang = planning.get("language") or _default_language()
    return lang if lang in _VALID_LANGUAGES else "en"


def planning_stats(planning: Dict[str, Any]) -> Dict[str, Any]:
    """Compute completion stats + a localized text rendering for a planning.

    For each event tagged with this planning, expand its occurrences within the
    planning's [period_start_utc, period_end_utc) window and classify each one:
    confirmed = success, everything else = failure. Aggregated per objective
    (event title) and overall.

    Returns {planning, overall, objectives:[...], text}.
    """
    period_start = recurrence._parse_utc(planning["period_start_utc"])
    period_end = recurrence._parse_utc(planning["period_end_utc"])

    objectives: List[Dict[str, Any]] = []
    overall_total = 0
    overall_confirmed = 0

    for ev in store.list_planning_events(planning["id"]):
        ev_local = dict(ev)
        try:
            ev_local["_exceptions"] = store.get_exceptions(ev["id"])
        except Exception:
            ev_local["_exceptions"] = set()
        try:
            occs = recurrence.occurrences(ev_local, period_start, period_end)
        except Exception:
            occs = []
        # Batch statuses once per event (avoid an N+1 get_status per occurrence).
        try:
            status_map = {s["occurrence_utc"]: s["status"] for s in store.list_statuses(ev["id"])}
        except Exception:
            status_map = {}
        total = 0
        confirmed = 0
        for occ in occs:
            total += 1
            if status_map.get(occ.isoformat()) == "confirmed":
                confirmed += 1
        failed = total - confirmed
        objectives.append({
            "title": ev["title"],
            "total": total,
            "confirmed": confirmed,
            "failed": failed,
        })
        overall_total += total
        overall_confirmed += confirmed

    overall_failed = overall_total - overall_confirmed
    completion_pct = (
        round(100 * overall_confirmed / overall_total) if overall_total else 0
    )
    overall = {
        "total": overall_total,
        "confirmed": overall_confirmed,
        "failed": overall_failed,
        "completion_pct": completion_pct,
    }

    text = _render_text(planning, overall, objectives)
    return {
        "planning": planning,
        "overall": overall,
        "objectives": objectives,
        "text": text,
    }


def _render_text(
    planning: Dict[str, Any],
    overall: Dict[str, Any],
    objectives: List[Dict[str, Any]],
) -> str:
    """Localized plain-text rendering of the report (EN/FR)."""
    lang = _planning_lang(planning)
    label = planning.get("period_label") or planning.get("name") or ""
    lines: List[str] = []
    if lang == "fr":
        lines.append(f"Rapport de planning — {label}")
        lines.append(
            f"Global : {overall['confirmed']}/{overall['total']} réalisés "
            f"({overall['completion_pct']}%)."
        )
        for obj in objectives:
            lines.append(f"• {obj['title']} : {obj['confirmed']}/{obj['total']}")
        lines.append(
            "Seuls les éléments confirmés comptent comme réalisés ; "
            "tout le reste compte comme non fait."
        )
    else:
        lines.append(f"Planning report — {label}")
        lines.append(
            f"Overall: {overall['confirmed']}/{overall['total']} completed "
            f"({overall['completion_pct']}%)."
        )
        for obj in objectives:
            lines.append(f"• {obj['title']}: {obj['confirmed']}/{obj['total']}")
        lines.append(
            "Only confirmed items count as completed; "
            "everything else counts as not done."
        )
    return "\n".join(lines)


def render_report_pdf(stats: Dict[str, Any], lang: str = "en") -> bytes | None:
    """Render the planning report as a styled A4 PDF (HTML/CSS via weasyprint).

    Returns the PDF bytes, or None if weasyprint is unavailable (so callers can
    transparently fall back to a text-only email). The PDF is self-contained:
    only inline CSS, no external resources/URLs, and a fixed print palette (a PDF
    is not themed).
    """
    try:
        from weasyprint import HTML
    except Exception:
        return None

    import html as _html
    from zoneinfo import ZoneInfo

    if lang not in _VALID_LANGUAGES:
        lang = "en"
    fr = lang == "fr"

    planning = stats.get("planning") or {}
    overall = stats.get("overall") or {}
    objectives = stats.get("objectives") or []

    name = planning.get("name") or ""
    period_label = planning.get("period_label") or name
    owner = planning.get("owner") or ""

    tz_name = planning.get("tz") or recurrence.DEFAULT_TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        try:
            tz = ZoneInfo(recurrence.DEFAULT_TZ)
        except Exception:
            tz = timezone.utc
    now_local = datetime.now(tz)
    gen_date = now_local.strftime("%Y-%m-%d %H:%M")

    pct = overall.get("completion_pct", 0) or 0
    o_total = overall.get("total", 0) or 0
    o_confirmed = overall.get("confirmed", 0) or 0

    # Localized labels.
    if fr:
        t_title = "Rapport de planning"
        t_completed = "réalisés"
        t_generated = f"généré le {gen_date}"
        t_col_obj = "Objectif"
        t_col_done = "Réalisé"
        t_col_prog = "Progression"
        t_footer = ("Seuls les éléments confirmés comptent comme réalisés ; "
                    "tout le reste compte comme non fait.")
    else:
        t_title = "Planning report"
        t_completed = "completed"
        t_generated = f"generated on {gen_date}"
        t_col_obj = "Objective"
        t_col_done = "Done"
        t_col_prog = "Progress"
        t_footer = ("Only confirmed items count as completed; "
                    "everything else counts as not done.")

    subline_parts = []
    if owner:
        subline_parts.append(_html.escape(str(owner)))
    subline_parts.append(t_generated)
    subline = " · ".join(subline_parts)

    rows: List[str] = []
    for obj in objectives:
        o_t = obj.get("total", 0) or 0
        o_c = obj.get("confirmed", 0) or 0
        o_pct = round(100 * o_c / o_t) if o_t else 0
        title = _html.escape(str(obj.get("title", "")))
        rows.append(
            "<tr>"
            f"<td class='obj'>{title}</td>"
            f"<td class='done'>{o_c}/{o_t}</td>"
            "<td class='prog'>"
            "<div class='bar bar-sm'>"
            f"<span style='width:{o_pct}%'></span>"
            "</div>"
            f"<span class='pct'>{o_pct}%</span>"
            "</td>"
            "</tr>"
        )
    rows_html = "".join(rows)

    html = f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 1.8cm; }}
  body {{ font-family: Helvetica, Arial, sans-serif; color: #1f2933; font-size: 13px; }}
  .header {{ border-bottom: 3px solid #2563eb; padding-bottom: 10px; margin-bottom: 22px; }}
  .header h1 {{ font-size: 22px; margin: 0 0 4px 0; color: #111827; }}
  .header .period {{ font-size: 15px; color: #374151; font-weight: 600; }}
  .header .subline {{ font-size: 11px; color: #6b7280; margin-top: 6px; }}
  .overall {{ margin-bottom: 26px; }}
  .overall .pct-big {{ font-size: 40px; font-weight: 700; color: #16a34a; line-height: 1; }}
  .overall .count {{ font-size: 14px; color: #374151; margin: 4px 0 12px 0; }}
  .bar {{ background: #e5e7eb; border-radius: 8px; height: 16px; overflow: hidden; width: 100%; }}
  .bar span {{ display: block; height: 100%; background: #16a34a; border-radius: 8px; }}
  .bar-sm {{ height: 8px; display: inline-block; width: 120px; vertical-align: middle; }}
  .bar-sm span {{ border-radius: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 22px; }}
  thead th {{
    text-transform: uppercase; font-size: 10px; letter-spacing: 0.04em;
    color: #6b7280; text-align: left; padding: 6px 8px; border-bottom: 2px solid #d1d5db;
  }}
  tbody td {{ padding: 8px; border-bottom: 1px solid #e5e7eb; vertical-align: middle; }}
  tbody tr:nth-child(even) {{ background: #f9fafb; }}
  td.done {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
  td.prog .pct {{ font-size: 11px; color: #6b7280; margin-left: 8px; }}
  .footer {{ font-size: 11px; color: #9ca3af; border-top: 1px solid #e5e7eb; padding-top: 10px; }}
</style>
</head>
<body>
  <div class="header">
    <h1>{_html.escape(t_title)}</h1>
    <div class="period">{_html.escape(str(period_label))}</div>
    <div class="subline">{subline}</div>
  </div>
  <div class="overall">
    <div class="pct-big">{pct}%</div>
    <div class="count">{o_confirmed}/{o_total} {t_completed}</div>
    <div class="bar"><span style="width:{pct}%"></span></div>
  </div>
  <table>
    <thead>
      <tr>
        <th>{_html.escape(t_col_obj)}</th>
        <th>{_html.escape(t_col_done)}</th>
        <th>{_html.escape(t_col_prog)}</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
  <div class="footer">{_html.escape(t_footer)}</div>
</body>
</html>"""

    return HTML(string=html).write_pdf()


def report_subject(planning: Dict[str, Any]) -> str:
    """Localized email subject for a planning report."""
    lang = _planning_lang(planning)
    name = planning.get("name") or ""
    if lang == "fr":
        return f"Rapport de planning : {name}"
    return f"Planning report: {name}"


def report_due_utc(planning: Dict[str, Any]) -> datetime:
    """09:00 (in the planning's tz) on the calendar date of period_end_utc.

    period_end is EXCLUSIVE (00:00 of the day after the last day), so its
    calendar date IS the morning after the period — exactly when the report is
    due. Returned as a tz-aware UTC datetime."""
    from zoneinfo import ZoneInfo

    tz_name = planning.get("tz") or recurrence.DEFAULT_TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    period_end = recurrence._parse_utc(planning["period_end_utc"])
    end_local = period_end.astimezone(tz)
    due_local = end_local.replace(hour=9, minute=0, second=0, microsecond=0)
    return due_local.astimezone(timezone.utc)
