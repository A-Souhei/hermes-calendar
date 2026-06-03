"""Job / time-tracking report builder and renderers.

Dependency-light: imports only store, recurrence (relative), and stdlib.
Mirrors planning.py / digest.py conventions.
"""

from __future__ import annotations

import html as _html
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import recurrence
from . import store

_VALID_LANGUAGES = ("en", "fr")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_hms(seconds: int) -> str:
    """Human duration: 2h 35m, 45m, 0m."""
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _default_language() -> str:
    env_lang = os.environ.get("CALENDAR_DEFAULT_LANG", "").strip().lower()
    return env_lang if env_lang in _VALID_LANGUAGES else "en"


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build_job_summary(
    owner: str,
    start_utc_iso: str,
    end_utc_iso: str,
    tz_name: str,
    *,
    category: Optional[str] = None,
    period_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Call store.summarize_jobs and bundle results with metadata.

    Returns::

        {
            "owner":        str,
            "tz":           str,
            "period_label": str | None,
            "start":        str,
            "end":          str,
            "category":     str | None,
            "jobs":         [...],
            "categories":   [...],
            "total_seconds": int,
            "count":        int,
        }
    """
    raw = store.summarize_jobs(owner, start_utc_iso, end_utc_iso, category=category)
    return {
        "owner": owner,
        "tz": tz_name,
        "period_label": period_label,
        "start": start_utc_iso,
        "end": end_utc_iso,
        "category": category,
        "jobs": raw["jobs"],
        "categories": raw["categories"],
        "total_seconds": raw["total_seconds"],
        "count": raw["count"],
    }


# ---------------------------------------------------------------------------
# Text renderer
# ---------------------------------------------------------------------------

def render_text(summary: Dict[str, Any]) -> str:
    """Plain-text job summary report."""
    lines: List[str] = []
    label = summary.get("period_label") or ""
    owner = summary.get("owner") or ""
    header = f"Job summary — {owner}"
    if label:
        header += f" · {label}"
    lines.append(header)
    lines.append("")

    jobs = summary.get("jobs") or []
    if jobs:
        lines.append("Jobs:")
        for j in jobs:
            cat = f" [{j['category']}]" if j.get("category") else ""
            lines.append(
                f"  • {j['job']}{cat}: {_fmt_hms(j['total_seconds'])} ({j['count']} session{'s' if j['count'] != 1 else ''})"
            )
    else:
        lines.append("  No tracked jobs in this period.")

    lines.append("")
    cats = summary.get("categories") or []
    if cats and len(cats) > 1:
        lines.append("By category:")
        for c in cats:
            cat_label = c["category"] or "(uncategorized)"
            lines.append(f"  • {cat_label}: {_fmt_hms(c['total_seconds'])}")
        lines.append("")

    lines.append(
        f"Total: {_fmt_hms(summary.get('total_seconds', 0))} "
        f"({summary.get('count', 0)} session{'s' if summary.get('count', 0) != 1 else ''})"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

def _job_rows_html(jobs: List[Dict[str, Any]]) -> str:
    rows: List[str] = []
    for j in jobs:
        job_name = _html.escape(str(j.get("job") or ""))
        cat = _html.escape(str(j["category"])) if j.get("category") else '<span style="color:#9ca3af;">—</span>'
        time_str = _html.escape(_fmt_hms(j.get("total_seconds", 0)))
        count = int(j.get("count", 0))
        rows.append(
            "<tr>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;'>{job_name}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;color:#6b7280;'>{cat}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;font-variant-numeric:tabular-nums;'>{time_str}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;color:#6b7280;'>{count}</td>"
            "</tr>"
        )
    return "".join(rows)


def render_html(summary: Dict[str, Any]) -> str:
    """Full HTML email document for a job summary, branded like digest._html_email."""
    owner = _html.escape(str(summary.get("owner") or ""))
    label = _html.escape(str(summary.get("period_label") or ""))
    total = _html.escape(_fmt_hms(summary.get("total_seconds", 0)))
    count = int(summary.get("count", 0))
    jobs = summary.get("jobs") or []
    cats = summary.get("categories") or []

    subline = owner
    if label:
        subline += f" · {label}"

    rows_html = _job_rows_html(jobs)
    no_jobs_html = "" if jobs else "<p style='color:#6b7280;margin:0;'>No tracked jobs in this period.</p>"

    cat_rows = ""
    if cats and len(cats) > 1:
        cat_items = "".join(
            f"<li style='margin:3px 0;'>"
            f"<strong>{_html.escape(str(c['category']) if c.get('category') else '(uncategorized)')}</strong>: "
            f"{_html.escape(_fmt_hms(c.get('total_seconds', 0)))}"
            f"</li>"
            for c in cats
        )
        cat_rows = (
            '<h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.04em;'
            'color:#374151;border-bottom:1px solid #e5e7eb;padding-bottom:5px;margin:20px 0 8px;">'
            "By category</h2>"
            f'<ul style="margin:0 0 6px;padding-left:18px;">{cat_items}</ul>'
        )

    body_html = (
        f'<h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.04em;'
        f'color:#374151;border-bottom:1px solid #e5e7eb;padding-bottom:5px;margin:0 0 8px;">'
        f"Jobs</h2>"
        + (
            f'<table style="width:100%;border-collapse:collapse;margin-bottom:16px;">'
            f'<thead><tr>'
            f'<th style="text-align:left;padding:6px 8px;font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;border-bottom:2px solid #d1d5db;">Job</th>'
            f'<th style="text-align:left;padding:6px 8px;font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;border-bottom:2px solid #d1d5db;">Category</th>'
            f'<th style="text-align:left;padding:6px 8px;font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;border-bottom:2px solid #d1d5db;">Time</th>'
            f'<th style="text-align:left;padding:6px 8px;font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;border-bottom:2px solid #d1d5db;">Sessions</th>'
            f'</tr></thead><tbody>{rows_html}</tbody></table>'
            if jobs else no_jobs_html
        )
        + cat_rows
        + f'<p style="margin:16px 0 4px;font-weight:600;">Total: {total} '
        f'({count} session{"s" if count != 1 else ""})</p>'
    )

    return (
        "<!doctype html><html><body "
        'style="margin:0;background:#f3f4f6;padding:24px;'
        "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        'color:#1f2933;">'
        '<div style="max-width:640px;margin:0 auto;background:#ffffff;'
        'border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;">'
        '<div style="background:linear-gradient(135deg,#1f2937,#111827);padding:18px 22px;">'
        '<div style="color:#ffffff;font-size:18px;font-weight:700;">\U0001f9f0 Job summary</div>'
        '<div style="color:#9ca3af;font-size:12px;margin-top:2px;">'
        + _html.escape(subline)
        + "</div></div>"
        '<div style="padding:18px 22px;font-size:14px;line-height:1.5;">'
        + body_html
        + "</div>"
        '<div style="padding:12px 22px;border-top:1px solid #e5e7eb;'
        'color:#9ca3af;font-size:11px;">Hermes · calendar-jobs</div>'
        "</div></body></html>"
    )


# ---------------------------------------------------------------------------
# PDF renderer
# ---------------------------------------------------------------------------

def render_pdf(summary: Dict[str, Any], lang: str = "en") -> Optional[bytes]:
    """A4 styled PDF via weasyprint; returns None if weasyprint is unavailable."""
    try:
        from weasyprint import HTML
    except Exception:
        return None

    from zoneinfo import ZoneInfo

    if lang not in _VALID_LANGUAGES:
        lang = "en"
    fr = lang == "fr"

    owner = summary.get("owner") or ""
    label = summary.get("period_label") or ""
    jobs = summary.get("jobs") or []
    total_seconds = summary.get("total_seconds", 0) or 0
    count = summary.get("count", 0) or 0

    tz_name = summary.get("tz") or recurrence.DEFAULT_TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        try:
            tz = ZoneInfo(recurrence.DEFAULT_TZ)
        except Exception:
            tz = timezone.utc
    now_local = datetime.now(tz)
    gen_date = now_local.strftime("%Y-%m-%d %H:%M")

    if fr:
        t_title = "Résumé des travaux"
        t_generated = f"généré le {gen_date}"
        t_col_job = "Travail"
        t_col_cat = "Catégorie"
        t_col_time = "Temps"
        t_col_count = "Sessions"
        t_total = "Total"
        t_no_jobs = "Aucun travail suivi sur cette période."
    else:
        t_title = "Job summary"
        t_generated = f"generated on {gen_date}"
        t_col_job = "Job"
        t_col_cat = "Category"
        t_col_time = "Time"
        t_col_count = "Sessions"
        t_total = "Total"
        t_no_jobs = "No tracked jobs in this period."

    subline_parts = []
    if owner:
        subline_parts.append(_html.escape(str(owner)))
    subline_parts.append(t_generated)
    subline = " · ".join(subline_parts)

    rows: List[str] = []
    for j in jobs:
        job_name = _html.escape(str(j.get("job") or ""))
        cat = _html.escape(str(j["category"])) if j.get("category") else "—"
        time_str = _fmt_hms(j.get("total_seconds", 0))
        n = int(j.get("count", 0))
        rows.append(
            "<tr>"
            f"<td class='job'>{job_name}</td>"
            f"<td class='cat'>{cat}</td>"
            f"<td class='time'>{time_str}</td>"
            f"<td class='cnt'>{n}</td>"
            "</tr>"
        )
    rows_html = "".join(rows)

    html_src = f"""<!DOCTYPE html>
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
  .total-block {{ margin-bottom: 26px; font-size: 15px; font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 22px; }}
  thead th {{
    text-transform: uppercase; font-size: 10px; letter-spacing: 0.04em;
    color: #6b7280; text-align: left; padding: 6px 8px; border-bottom: 2px solid #d1d5db;
  }}
  tbody td {{ padding: 8px; border-bottom: 1px solid #e5e7eb; vertical-align: middle; }}
  tbody tr:nth-child(even) {{ background: #f9fafb; }}
  td.time {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
  td.cnt {{ color: #6b7280; }}
  td.cat {{ color: #6b7280; }}
  .footer {{ font-size: 11px; color: #9ca3af; border-top: 1px solid #e5e7eb; padding-top: 10px; }}
  .no-jobs {{ color: #6b7280; font-style: italic; }}
</style>
</head>
<body>
  <div class="header">
    <h1>{_html.escape(t_title)}</h1>
    <div class="period">{_html.escape(str(label))}</div>
    <div class="subline">{subline}</div>
  </div>
  <div class="total-block">
    {_html.escape(t_total)}: {_fmt_hms(total_seconds)}
    ({count} session{"s" if count != 1 else ""})
  </div>
  {'<p class="no-jobs">' + _html.escape(t_no_jobs) + '</p>' if not jobs else
  '<table><thead><tr>'
  + f'<th>{_html.escape(t_col_job)}</th>'
  + f'<th>{_html.escape(t_col_cat)}</th>'
  + f'<th>{_html.escape(t_col_time)}</th>'
  + f'<th>{_html.escape(t_col_count)}</th>'
  + '</tr></thead><tbody>'
  + rows_html
  + '</tbody></table>'}
</body>
</html>"""

    return HTML(string=html_src).write_pdf()


# ---------------------------------------------------------------------------
# Email subject
# ---------------------------------------------------------------------------

def report_subject(summary: Dict[str, Any], lang: str = "en") -> str:
    """Localized email subject for a job summary report."""
    if lang not in _VALID_LANGUAGES:
        lang = _default_language()
    owner = summary.get("owner") or ""
    label = summary.get("period_label") or ""
    if lang == "fr":
        s = f"Résumé des travaux : {owner}"
        if label:
            s += f" — {label}"
        return s
    s = f"Job summary: {owner}"
    if label:
        s += f" — {label}"
    return s
