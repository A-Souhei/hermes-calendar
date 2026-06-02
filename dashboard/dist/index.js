/*
 * Calendar — Hermes dashboard plugin tab (read-only).
 *
 * Plain-JS IIFE rendered by the Hermes dashboard host. It uses the host SDK at
 * window.__HERMES_PLUGIN_SDK__ (React + a small shadcn-style component/util set)
 * and registers itself via window.__HERMES_PLUGINS__.register("calendar", ...).
 *
 * NO build step. All data comes from the read-only backend at
 * /api/plugins/calendar/ (plugin_api.py). Editing is done by talking to the
 * agent — this view never mutates.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const { Card, CardContent, Badge, Button } = SDK.components;
  const { useState, useEffect, useCallback, useMemo } = SDK.hooks;
  const cn = (SDK.utils && SDK.utils.cn) || function () {
    return Array.prototype.filter.call(arguments, Boolean).join(" ");
  };

  const API = "/api/plugins/calendar";
  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const MAX_CHIPS = 3;

  // --- status helpers -------------------------------------------------------

  var STATUS_GLYPH = { confirmed: "✓", active: "●", missed: "✗" };

  function statusGlyph(status) {
    return STATUS_GLYPH[status] || null;
  }

  // Effective display status (backend-derived): a past, still-floating
  // occurrence reads as "missed" while staying floating underneath.
  function effStatus(ev) {
    return ev.effective_status || ev.status || "floating";
  }
  // True when "missed" is only inferred from the past date, not explicitly set.
  function isDerivedMiss(ev) {
    return effStatus(ev) === "missed" && (ev.status === "floating" || !ev.status);
  }

  function fmtDuration(seconds) {
    if (seconds == null || seconds < 0) return null;
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = seconds % 60;
    if (h > 0 && m > 0) return h + "h " + m + "m";
    if (h > 0) return h + "h";
    if (m > 0 && s > 0) return m + "m " + s + "s";
    if (m > 0) return m + "m";
    return s + "s";
  }

  function fmtElapsed(startedUtc) {
    if (!startedUtc) return null;
    try {
      var elapsed = Math.max(0, Math.round((Date.now() - new Date(startedUtc).getTime()) / 1000));
      return fmtDuration(elapsed);
    } catch (e) {
      return null;
    }
  }

  // --- date helpers ---------------------------------------------------------

  function pad2(n) { return String(n).padStart(2, "0"); }

  function isoDate(d) {
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  function addDays(d, n) {
    const x = new Date(d);
    x.setDate(x.getDate() + n);
    return x;
  }

  function fmtDateTime(iso, tz, allDay) {
    try {
      const d = new Date(iso);
      const opts = allDay
        ? { weekday: "short", year: "numeric", month: "short", day: "numeric" }
        : { weekday: "short", year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
      if (tz) opts.timeZone = tz;
      return d.toLocaleString(undefined, opts);
    } catch (e) {
      return iso || "";
    }
  }

  // The first-of-month Date for the currently displayed month.
  function monthAnchor(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }

  // 42-cell grid (6 weeks, Monday-first) starting on/before the 1st.
  function gridStartFor(anchor) {
    const offset = (anchor.getDay() + 6) % 7; // 0=Mon ... 6=Sun
    return addDays(anchor, -offset);
  }

  // --- data layer -----------------------------------------------------------

  function useEvents(anchor) {
    const [events, setEvents] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    const load = useCallback(() => {
      const start = gridStartFor(anchor);
      // Pad ±1 day so events near a tz/midnight boundary aren't dropped by the
      // UTC range; the frontend buckets by the event's own local date anyway.
      const from = isoDate(addDays(start, -1)) + "T00:00:00";
      const to = isoDate(addDays(start, 43)) + "T00:00:00";
      setLoading(true);
      setError(null);
      SDK.fetchJSON(API + "/events?from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to))
        .then(function (data) {
          setEvents((data && data.events) || []);
          setLoading(false);
        })
        .catch(function (err) {
          setError((err && err.message) || "Failed to load events");
          setLoading(false);
        });
    }, [anchor]);

    useEffect(load, [load]);
    return { events, loading, error, reload: load };
  }

  // Upcoming occurrences (used for the "Next up" chips). Tolerant of failure:
  // resolves to an empty list rather than surfacing an error.
  function useUpcoming(days) {
    const [items, setItems] = useState([]);
    useEffect(function () {
      let alive = true;
      SDK.fetchJSON(API + "/upcoming?days=" + encodeURIComponent(days))
        .then(function (data) { if (alive) setItems((data && data.events) || []); })
        .catch(function () { if (alive) setItems([]); });
      return function () { alive = false; };
    }, [days]);
    return items;
  }

  function fmtTime(iso, tz) {
    try {
      const d = new Date(iso);
      const opts = { hour: "2-digit", minute: "2-digit" };
      if (tz) opts.timeZone = tz;
      return d.toLocaleTimeString(undefined, opts);
    } catch (e) {
      return "";
    }
  }

  // --- chips & cells --------------------------------------------------------

  function EventChip(props) {
    const ev = props.event;
    const eff = effStatus(ev);
    const derived = isDerivedMiss(ev);
    const glyph = statusGlyph(eff);
    const prefix = (glyph ? h("span", { className: cn("cal-status-glyph", derived && "cal-status-derived") }, glyph) : null);
    const planGlyph = ev.planning ? h("span", { className: "cal-plan-glyph" }, "🗜️") : null;
    return h(
      "button",
      {
        className: cn("cal-chip", ev.recurring ? "cal-chip-recurring" : "cal-chip-once"),
        title: ev.title + (ev.recurrence_human ? " · " + ev.recurrence_human : "") + (eff !== "floating" ? " · " + (derived ? "missed (unconfirmed)" : eff) : "") + (ev.planning ? " · plan: " + ev.planning : ""),
        onClick: function () { props.onOpen(ev.id); },
      },
      prefix,
      planGlyph,
      h("span", { className: "cal-chip-title" }, (ev.has_report ? "📝 " : "") + ev.title)
    );
  }

  function DayCell(props) {
    const { date, inMonth, isToday, events, onOpen, onOpenDay } = props;
    const shown = events.slice(0, MAX_CHIPS);
    const extra = events.length - shown.length;
    const dayNum = h(
      "button",
      {
        className: "cal-daynum",
        title: events.length ? "View all events on this day" : undefined,
        onClick: function () { if (events.length) onOpenDay(date, events); },
      },
      String(date.getDate())
    );
    return h(
      "div",
      { className: cn("cal-cell", !inMonth && "cal-cell-muted", isToday && "cal-cell-today") },
      dayNum,
      shown.map(function (ev, i) {
        return h(EventChip, { key: ev.id + "@" + ev.occurrence_utc + i, event: ev, onOpen: onOpen });
      }),
      extra > 0
        ? h(
            "button",
            { className: "cal-more", onClick: function () { onOpenDay(date, events); } },
            "+" + extra + " more"
          )
        : null
    );
  }

  // Day view — lists ALL events for one day; each row opens the detail modal.
  function DayModal(props) {
    const date = props.date;
    const events = props.events || [];
    const label = date.toLocaleDateString(undefined, {
      weekday: "long", year: "numeric", month: "long", day: "numeric",
    });
    return h(
      "div",
      {
        className: "fixed inset-0 z-50 flex items-start justify-center p-4 sm:p-8 bg-black/60 cal-overlay",
        onClick: function (e) { if (e.target === e.currentTarget) props.onClose(); },
      },
      h(
        "div",
        { className: "cal-modal w-full max-w-md shadow-xl" },
        h(
          "div",
          { className: "cal-modal-body p-5 space-y-3" },
          h(
            "div",
            { className: "cal-modal-head flex items-start justify-between gap-3" },
            h("h2", { className: "text-base font-semibold leading-tight" }, label),
            h(Button, { variant: "ghost", size: "sm", onClick: props.onClose }, "✕")
          ),
          events.length === 0
            ? h("div", { className: "text-sm opacity-60" }, "No events.")
            : h(
                "div",
                { className: "space-y-1" },
                events.map(function (ev, i) {
                  const derived = isDerivedMiss(ev);
                  const glyph = statusGlyph(effStatus(ev));
                  const timeStr = ev.all_day ? "All day" : fmtTime(ev.occurrence_local, ev.tz);
                  const dur = ev.duration_seconds != null ? fmtDuration(ev.duration_seconds) : null;
                  const durStr = dur ? " · " + dur : "";
                  return h(
                    "button",
                    {
                      key: ev.id + "@" + ev.occurrence_utc + i,
                      className: cn("cal-chip cal-dayrow", ev.recurring ? "cal-chip-recurring" : "cal-chip-once"),
                      title: ev.title + (ev.recurrence_human ? " · " + ev.recurrence_human : "") + (ev.planning ? " · plan: " + ev.planning : ""),
                      onClick: function () { props.onOpenEvent(ev.id); },
                    },
                    glyph ? h("span", { className: cn("cal-status-glyph", derived && "cal-status-derived") }, glyph) : null,
                    ev.planning ? h("span", { className: "cal-plan-glyph" }, "🗜️") : null,
                    h("span", { className: "cal-chip-title" }, (ev.has_report ? "📝 " : "") + timeStr + " · " + ev.title + durStr)
                  );
                })
              )
        )
      )
    );
  }

  // --- detail modal ---------------------------------------------------------

  function ReportField(props) {
    const key = props.k;
    const val = props.v;
    if (val == null || val === "" || (Array.isArray(val) && val.length === 0)) return null;
    const label = key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, " ");

    let body;
    if (key === "transcription" || key === "transcript") {
      body = h("div", { className: "cal-transcript" }, String(val));
    } else if (Array.isArray(val)) {
      body = h(
        "ul",
        { className: "list-disc pl-5 space-y-1 text-sm" },
        val.map(function (item, i) {
          return h("li", { key: i }, typeof item === "string" ? item : JSON.stringify(item));
        })
      );
    } else if (typeof val === "object") {
      body = h("pre", { className: "cal-transcript" }, JSON.stringify(val, null, 2));
    } else {
      body = h("div", { className: "text-sm whitespace-pre-wrap" }, String(val));
    }

    return h(
      "div",
      { className: "space-y-1" },
      h("div", { className: "text-xs font-semibold uppercase tracking-wide opacity-60" }, label),
      body
    );
  }

  function ReportBlock(props) {
    const r = props.report;
    const rep = r.report || {};
    const keys = Object.keys(rep);
    return h(
      "div",
      { className: "rounded-md border p-3 space-y-3" },
      h(
        "div",
        { className: "flex items-center justify-between gap-2" },
        h("div", { className: "text-sm font-medium" }, fmtDateTime(r.occurrence_local || r.occurrence_utc, props.tz)),
        r.updated_utc ? h("span", { className: "text-xs opacity-50" }, "updated " + fmtDateTime(r.updated_utc, props.tz)) : null
      ),
      keys.length === 0
        ? h("div", { className: "text-sm opacity-60" }, "Empty report.")
        : h(
            "div",
            { className: "space-y-3" },
            keys.map(function (k) { return h(ReportField, { key: k, k: k, v: rep[k] }); })
          )
    );
  }

  function KV(props) {
    if (props.value == null || props.value === "") return null;
    return h(
      "div",
      { className: "contents" },
      h("dt", null, props.label),
      h("dd", null, props.value)
    );
  }

  function DetailModal(props) {
    const id = props.eventId;
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    useEffect(function () {
      let alive = true;
      setLoading(true);
      setError(null);
      SDK.fetchJSON(API + "/event/" + encodeURIComponent(id))
        .then(function (d) { if (alive) { setData(d); setLoading(false); } })
        .catch(function (err) { if (alive) { setError((err && err.message) || "Failed to load"); setLoading(false); } });
      return function () { alive = false; };
    }, [id]);

    const meeting = data && data.meeting;
    const tags = (data && data.tags) || [];

    return h(
      "div",
      {
        className: "fixed inset-0 z-50 flex items-start justify-center p-4 sm:p-8 bg-black/60 cal-overlay",
        onClick: function (e) { if (e.target === e.currentTarget) props.onClose(); },
      },
      h(
        "div",
        { className: "cal-modal w-full max-w-2xl shadow-xl" },
        h(
          "div",
          { className: "cal-modal-body p-5 space-y-4" },
          h(
            "div",
            { className: "cal-modal-head flex items-start justify-between gap-3" },
            h(
              "div",
              { className: "space-y-1" },
              h("h2", { className: "text-lg font-semibold leading-tight" }, data ? data.title : "Loading…"),
              data && data.recurrence_human
                ? h(Badge, { variant: "secondary" }, "↻ " + data.recurrence_human)
                : null
            ),
            h(Button, { variant: "ghost", size: "sm", onClick: props.onClose }, "✕")
          ),

          loading ? h("div", { className: "text-sm opacity-60 py-6" }, "Loading…") : null,
          error ? h("div", { className: "text-sm text-red-600 py-6" }, error) : null,

          data
            ? h(
                "div",
                { className: "space-y-4" },
                h(
                  "dl",
                  { className: "cal-kv" },
                  h(KV, { label: "When", value: fmtDateTime(data.start_utc, data.tz, data.all_day) + (data.all_day ? " (all day)" : "") }),
                  data.planning
                    ? h(KV, { label: "Planning", value: h(Badge, { variant: "secondary" }, "🗜️ " + data.planning) })
                    : null,
                  h(KV, { label: "Timezone", value: data.tz }),
                  h(KV, { label: "Location", value: data.location }),
                  h(KV, { label: "Alert", value: data.alert_channel }),
                  data.alert_lead_seconds != null
                    ? h(KV, { label: "Lead", value: Math.round(data.alert_lead_seconds / 60) + " min before" })
                    : null
                ),

                data.description
                  ? h(
                      "div",
                      { className: "space-y-1" },
                      h("div", { className: "text-xs font-semibold uppercase tracking-wide opacity-60" }, "Description"),
                      h("div", { className: "text-sm whitespace-pre-wrap" }, data.description)
                    )
                  : null,

                meeting && (meeting.room_url || meeting.room_app || (meeting.participants && meeting.participants.length))
                  ? h(
                      "div",
                      { className: "rounded-md border p-3 space-y-2" },
                      h("div", { className: "text-xs font-semibold uppercase tracking-wide opacity-60" }, "Meeting"),
                      meeting.room_app ? h("div", { className: "text-sm" }, "App: " + meeting.room_app) : null,
                      meeting.room_url
                        ? (/^https?:\/\//i.test(meeting.room_url)
                            ? h("a", { className: "text-sm text-blue-600 underline break-all", href: meeting.room_url, target: "_blank", rel: "noreferrer" }, meeting.room_url)
                            : h("div", { className: "text-sm break-all" }, meeting.room_url))
                        : null,
                      meeting.participants && meeting.participants.length
                        ? h(
                            "div",
                            { className: "text-sm" },
                            "Participants: " + meeting.participants.join(", ")
                          )
                        : null
                    )
                  : null,

                tags.length
                  ? h(
                      "div",
                      { className: "flex flex-wrap gap-1" },
                      tags.map(function (t, i) { return h(Badge, { key: i, variant: "outline" }, t); })
                    )
                  : null,

                data.statuses && data.statuses.length
                  ? h(
                      "div",
                      { className: "space-y-2" },
                      h("div", { className: "text-sm font-semibold" }, "Status history (" + data.statuses.length + ")"),
                      data.statuses.map(function (s) {
                        var dur = s.duration_seconds != null ? fmtDuration(s.duration_seconds) : null;
                        var running = s.status === "active" && s.started_utc ? fmtElapsed(s.started_utc) : null;
                        return h(
                          "div",
                          { key: s.occurrence_utc, className: "rounded-md border p-3 space-y-1 text-sm" },
                          h(
                            "div",
                            { className: "flex items-center justify-between gap-2 flex-wrap" },
                            h("span", { className: "font-medium" }, fmtDateTime(s.occurrence_local || s.occurrence_utc, data.tz)),
                            h("span", { className: "cal-status cal-status-" + s.status }, (STATUS_GLYPH[s.status] || "") + " " + s.status)
                          ),
                          s.started_utc
                            ? h("div", { className: "text-xs opacity-60" }, "Started: " + fmtDateTime(s.started_utc, data.tz))
                            : null,
                          s.ended_utc
                            ? h("div", { className: "text-xs opacity-60" }, "Ended: " + fmtDateTime(s.ended_utc, data.tz))
                            : null,
                          dur
                            ? h("div", { className: "text-xs opacity-70" }, "Duration: " + dur)
                            : null,
                          running
                            ? h("div", { className: "text-xs font-medium", style: { color: "var(--color-primary)" } }, "Running for " + running)
                            : null,
                          s.note
                            ? h("div", { className: "text-xs opacity-70 italic" }, s.note)
                            : null
                        );
                      })
                    )
                  : null,

                h(
                  "div",
                  { className: "space-y-2" },
                  h(
                    "div",
                    { className: "text-sm font-semibold" },
                    "Reports" + (data.reports && data.reports.length ? " (" + data.reports.length + ")" : "")
                  ),
                  data.reports && data.reports.length
                    ? data.reports.map(function (r) {
                        return h(ReportBlock, { key: r.occurrence_utc, report: r, tz: data.tz });
                      })
                    : h("div", { className: "text-sm opacity-60" }, "No reports yet.")
                ),

                h("div", { className: "text-xs opacity-40 pt-2 border-t" }, "Read-only — edits are made by talking to the assistant.")
              )
            : null
        )
      )
    );
  }

  // --- stat cards -----------------------------------------------------------

  function StatCard(props) {
    return h(
      "div",
      { className: "cal-stat" },
      h("div", { className: "cal-stat-label" }, props.label),
      h("div", { className: "cal-stat-value" }, props.value)
    );
  }

  // --- month-grid calendar view --------------------------------------------

  function CalendarView() {
    const [anchor, setAnchor] = useState(function () { return monthAnchor(new Date()); });
    const [openId, setOpenId] = useState(null);
    const [openDay, setOpenDay] = useState(null);
    const { events, loading, error, reload } = useEvents(anchor);
    const upcoming = useUpcoming(30);

    const byDate = useMemo(function () {
      const m = {};
      (events || []).forEach(function (ev) {
        const key = (ev.occurrence_local || ev.occurrence_utc || "").slice(0, 10);
        if (!key) return;
        (m[key] = m[key] || []).push(ev);
      });
      return m;
    }, [events]);

    const cells = useMemo(function () {
      const start = gridStartFor(anchor);
      const todayKey = isoDate(new Date());
      const arr = [];
      for (let i = 0; i < 42; i++) {
        const date = addDays(start, i);
        const key = isoDate(date);
        arr.push({
          date: date,
          key: key,
          inMonth: date.getMonth() === anchor.getMonth(),
          isToday: key === todayKey,
          events: byDate[key] || [],
        });
      }
      return arr;
    }, [anchor, byDate]);

    const monthLabel = anchor.toLocaleString(undefined, { month: "long", year: "numeric" });

    // At-a-glance stats over the currently-loaded month occurrences.
    const stats = useMemo(function () {
      var s = { total: 0, confirmed: 0, missed: 0, upcoming: 0, active: 0 };
      (events || []).forEach(function (ev) {
        s.total++;
        if (ev.status === "confirmed") s.confirmed++;
        if (ev.status === "active") s.active++;
        var eff = effStatus(ev);
        if (eff === "missed") s.missed++;
        if (eff === "floating") s.upcoming++;
      });
      return s;
    }, [events]);

    // First few future occurrences for the "Next up" chips.
    const nextUp = useMemo(function () {
      var nowMs = Date.now();
      return (upcoming || [])
        .filter(function (ev) {
          var t = new Date(ev.occurrence_utc || ev.occurrence_local || 0).getTime();
          return !isNaN(t) && t >= nowMs;
        })
        .sort(function (a, b) {
          return new Date(a.occurrence_utc || a.occurrence_local || 0) - new Date(b.occurrence_utc || b.occurrence_local || 0);
        })
        .slice(0, 6);
    }, [upcoming]);

    return h(
      "div",
      { className: "p-4 sm:p-6 space-y-4" },
      // month nav
      h(
        "div",
        { className: "flex items-center justify-between gap-3 flex-wrap" },
        h("span", { className: "text-sm font-medium opacity-70" }, monthLabel),
        h(
          "div",
          { className: "flex items-center gap-2" },
          h(Button, { variant: "outline", size: "sm", onClick: function () { setAnchor(new Date(anchor.getFullYear(), anchor.getMonth() - 1, 1)); } }, "◀"),
          h(Button, { variant: "outline", size: "sm", onClick: function () { setAnchor(monthAnchor(new Date())); } }, "Today"),
          h(Button, { variant: "outline", size: "sm", onClick: function () { setAnchor(new Date(anchor.getFullYear(), anchor.getMonth() + 1, 1)); } }, "▶"),
          h(Button, { variant: "ghost", size: "sm", onClick: reload, title: "Refresh" }, "⟳")
        )
      ),

      // at-a-glance stat cards
      h(
        "div",
        { className: "cal-statrow" },
        h(StatCard, { label: "Events", value: stats.total }),
        h(StatCard, { label: "Confirmed", value: stats.confirmed }),
        h(StatCard, { label: "Missed", value: stats.missed }),
        h(StatCard, { label: "Upcoming", value: stats.upcoming }),
        h(StatCard, { label: "Active", value: stats.active })
      ),

      // next up chips
      nextUp.length
        ? h(
            "div",
            { className: "space-y-1" },
            h("div", { className: "cal-section-label" }, "Next up"),
            h(
              "div",
              { className: "cal-nextup" },
              nextUp.map(function (ev, i) {
                var glyph = statusGlyph(effStatus(ev));
                var derived = isDerivedMiss(ev);
                var when = fmtDateTime(ev.occurrence_local || ev.occurrence_utc, ev.tz, ev.all_day)
                  .replace(/^\w+,?\s*/, "");
                return h(
                  "button",
                  {
                    key: ev.id + "@" + ev.occurrence_utc + i,
                    className: "cal-nextup-chip",
                    title: ev.title + " · " + fmtDateTime(ev.occurrence_local || ev.occurrence_utc, ev.tz, ev.all_day) + (ev.planning ? " · plan: " + ev.planning : ""),
                    onClick: function () { setOpenId(ev.id); },
                  },
                  glyph ? h("span", { className: cn("cal-status-glyph", derived && "cal-status-derived") }, glyph) : null,
                  ev.planning ? h("span", { className: "cal-plan-glyph" }, "🗜️") : null,
                  h("span", { className: "cal-nextup-when" }, when),
                  h("span", { className: "cal-nextup-title" }, ev.title)
                );
              })
            )
          )
        : null,

      error ? h("div", { className: "text-sm text-red-600" }, "⚠ " + error) : null,

      h(
        Card,
        null,
        h(
          CardContent,
          { className: "p-3" },
          // weekday header
          h(
            "div",
            { className: "cal-grid" },
            WEEKDAYS.map(function (d) { return h("div", { key: d, className: "cal-weekday" }, d); })
          ),
          // day grid
          h(
            "div",
            { className: cn("cal-grid", loading && "opacity-50") },
            cells.map(function (c) {
              return h(DayCell, {
                key: c.key,
                date: c.date,
                inMonth: c.inMonth,
                isToday: c.isToday,
                events: c.events,
                onOpen: setOpenId,
                onOpenDay: function (date, evs) { setOpenDay({ date: date, events: evs }); },
              });
            })
          )
        )
      ),

      // legend
      h(
        "div",
        { className: "flex items-center gap-4 flex-wrap text-xs opacity-60" },
        h("span", null, h("span", { className: "cal-chip cal-chip-once", style: { padding: "1px 6px" } }, "one-time")),
        h("span", null, h("span", { className: "cal-chip cal-chip-recurring", style: { padding: "1px 6px" } }, "recurring")),
        h("span", null, "📝 has report"),
        h("span", null, h("span", { className: "cal-status-glyph" }, "✓"), " confirmed"),
        h("span", null, h("span", { className: "cal-status-glyph" }, "●"), " active timer"),
        h("span", null, h("span", { className: "cal-status-glyph" }, "✗"), " missed")
      ),

      openDay ? h(DayModal, {
        date: openDay.date,
        events: openDay.events,
        onOpenEvent: function (id) { setOpenDay(null); setOpenId(id); },
        onClose: function () { setOpenDay(null); },
      }) : null,

      openId ? h(DetailModal, { eventId: openId, onClose: function () { setOpenId(null); } }) : null
    );
  }

  // --- plannings ------------------------------------------------------------

  function ProgressBar(props) {
    var pct = Math.max(0, Math.min(100, Number(props.pct) || 0));
    return h(
      "div",
      { className: "cal-progress" },
      h("div", { className: "cal-progress-fill", style: { width: pct + "%" } })
    );
  }

  function PlanningDetail(props) {
    const id = props.planningId;
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    useEffect(function () {
      let alive = true;
      setLoading(true);
      setError(null);
      SDK.fetchJSON(API + "/planning/" + encodeURIComponent(id))
        .then(function (d) { if (alive) { setData(d); setLoading(false); } })
        .catch(function (err) { if (alive) { setError((err && err.message) || "Failed to load"); setLoading(false); } });
      return function () { alive = false; };
    }, [id]);

    const overall = (data && data.overall) || {};
    const objectives = (data && data.objectives) || [];
    const events = (data && data.events) || [];

    return h(
      "div",
      {
        className: "fixed inset-0 z-50 flex items-start justify-center p-4 sm:p-8 bg-black/60 cal-overlay",
        onClick: function (e) { if (e.target === e.currentTarget) props.onClose(); },
      },
      h(
        "div",
        { className: "cal-modal w-full max-w-2xl shadow-xl" },
        h(
          "div",
          { className: "cal-modal-body p-5 space-y-4" },
          h(
            "div",
            { className: "cal-modal-head flex items-start justify-between gap-3" },
            h(
              "div",
              { className: "space-y-1" },
              h("h2", { className: "text-lg font-semibold leading-tight" }, data ? "🗜️ " + data.name : "Loading…"),
              data && data.period_label ? h("span", { className: "text-sm opacity-60" }, data.period_label) : null
            ),
            h(Button, { variant: "ghost", size: "sm", onClick: props.onClose }, "✕")
          ),

          loading ? h("div", { className: "text-sm opacity-60 py-6" }, "Loading…") : null,
          error ? h("div", { className: "text-sm text-red-600 py-6" }, error) : null,

          data
            ? h(
                "div",
                { className: "space-y-4" },
                data.description
                  ? h("div", { className: "text-sm whitespace-pre-wrap opacity-80" }, data.description)
                  : null,

                // overall progress
                h(
                  "div",
                  { className: "space-y-1" },
                  h(
                    "div",
                    { className: "flex items-center justify-between gap-2 text-sm" },
                    h("span", { className: "font-medium" }, "Overall"),
                    h("span", { className: "opacity-70" }, (overall.confirmed || 0) + "/" + (overall.total || 0) + " completed (" + (overall.completion_pct || 0) + "%)")
                  ),
                  h(ProgressBar, { pct: overall.completion_pct })
                ),

                // per-objective
                objectives.length
                  ? h(
                      "div",
                      { className: "space-y-2" },
                      h("div", { className: "text-sm font-semibold" }, "Objectives (" + objectives.length + ")"),
                      objectives.map(function (o, i) {
                        var total = o.total || 0;
                        var conf = o.confirmed || 0;
                        var pct = total > 0 ? Math.round((conf / total) * 100) : 0;
                        return h(
                          "div",
                          { key: i, className: "space-y-1" },
                          h(
                            "div",
                            { className: "flex items-center justify-between gap-2 text-sm" },
                            h("span", null, o.title),
                            h("span", { className: "opacity-70 text-xs" }, conf + "/" + total)
                          ),
                          h(ProgressBar, { pct: pct })
                        );
                      })
                    )
                  : null,

                // events
                events.length
                  ? h(
                      "div",
                      { className: "space-y-2" },
                      h("div", { className: "text-sm font-semibold" }, "Events (" + events.length + ")"),
                      h(
                        "div",
                        { className: "space-y-1" },
                        events.map(function (ev) {
                          return h(
                            "div",
                            { key: ev.id, className: "rounded-md border p-2 text-sm space-y-0.5" },
                            h("div", { className: "font-medium" }, ev.title),
                            h(
                              "div",
                              { className: "text-xs opacity-60" },
                              fmtDateTime(ev.start_utc, null, ev.all_day) + (ev.recurrence_human ? " · " + ev.recurrence_human : "")
                            )
                          );
                        })
                      )
                    )
                  : null,

                // report preview
                h(
                  "div",
                  { className: "space-y-1" },
                  h(
                    "div",
                    { className: "flex items-center justify-between gap-2" },
                    h("div", { className: "text-sm font-semibold" }, "Report"),
                    h(Badge, { variant: data.report_sent ? "secondary" : "outline" }, data.report_sent ? "report sent" : "pending")
                  ),
                  data.report_text
                    ? h("div", { className: "cal-transcript" }, String(data.report_text))
                    : h("div", { className: "text-sm opacity-60" }, "No report yet."),
                  h("div", { className: "text-xs opacity-40" }, "Reports are emailed to the owner; this is a preview.")
                ),

                h("div", { className: "text-xs opacity-40 pt-2 border-t" }, "Read-only — edits are made by talking to the assistant.")
              )
            : null
        )
      )
    );
  }

  // Classify a planning by its period relative to now.
  function planningStatus(p, now) {
    var s = p.period_start_utc ? new Date(p.period_start_utc).getTime() : null;
    var e = p.period_end_utc ? new Date(p.period_end_utc).getTime() : null;
    if (e != null && now >= e) return "past";
    if (s != null && now < s) return "upcoming";
    return "active";
  }
  var PLAN_FILTERS = [["all", "All"], ["active", "Active"], ["upcoming", "Upcoming"], ["past", "Past"]];

  function PlanningsView() {
    const [plannings, setPlannings] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [openId, setOpenId] = useState(null);
    const [filter, setFilter] = useState("all");

    const load = useCallback(function () {
      setLoading(true);
      setError(null);
      SDK.fetchJSON(API + "/plannings")
        .then(function (data) {
          setPlannings((data && data.plannings) || []);
          setLoading(false);
        })
        .catch(function (err) {
          setError((err && err.message) || "Failed to load plannings");
          setLoading(false);
        });
    }, []);

    useEffect(load, [load]);

    const now = Date.now();
    // newest-first by period start
    const sorted = useMemo(function () {
      return (plannings || []).slice().sort(function (a, b) {
        return new Date(b.period_start_utc || 0) - new Date(a.period_start_utc || 0);
      });
    }, [plannings]);
    const counts = useMemo(function () {
      var c = { all: sorted.length, active: 0, upcoming: 0, past: 0 };
      sorted.forEach(function (p) { c[planningStatus(p, now)]++; });
      return c;
    }, [sorted, now]);
    const shown = filter === "all" ? sorted : sorted.filter(function (p) { return planningStatus(p, now) === filter; });

    // At-a-glance stats over loaded plannings.
    const avgCompletion = useMemo(function () {
      var withPct = sorted.filter(function (p) {
        var ov = p.overall || {};
        return ov.completion_pct != null;
      });
      if (!withPct.length) return "—";
      var sum = withPct.reduce(function (acc, p) { return acc + (Number((p.overall || {}).completion_pct) || 0); }, 0);
      return Math.round(sum / withPct.length) + "%";
    }, [sorted]);

    return h(
      "div",
      { className: "p-4 sm:p-6 space-y-4" },
      h(
        "div",
        { className: "flex items-center justify-end gap-3 flex-wrap" },
        h(Button, { variant: "ghost", size: "sm", onClick: load, title: "Refresh" }, "⟳")
      ),

      error ? h("div", { className: "text-sm text-red-600" }, "⚠ " + error) : null,
      loading ? h("div", { className: "text-sm opacity-60" }, "Loading…") : null,

      // filter bar (newest-first; counts per bucket)
      !loading && sorted.length
        ? h(
            "div",
            { className: "cal-tabs" },
            PLAN_FILTERS.map(function (f) {
              return h(
                "button",
                {
                  key: f[0],
                  className: cn("cal-tab", filter === f[0] && "cal-tab-active"),
                  onClick: function () { setFilter(f[0]); },
                },
                f[1] + " (" + (counts[f[0]] || 0) + ")"
              );
            })
          )
        : null,

      // at-a-glance stat cards
      !loading && sorted.length
        ? h(
            "div",
            { className: "cal-statrow" },
            h(StatCard, { label: "Plannings", value: counts.all }),
            h(StatCard, { label: "Active", value: counts.active }),
            h(StatCard, { label: "Avg completion", value: avgCompletion })
          )
        : null,

      !loading && !error && sorted.length === 0
        ? h("div", { className: "text-sm opacity-60" }, "No plannings yet — ask the assistant to create one.")
        : null,
      !loading && sorted.length && shown.length === 0
        ? h("div", { className: "text-sm opacity-60" }, "No " + filter + " plannings.")
        : null,

      !loading && shown.length
        ? h(
            "div",
            { className: "space-y-3" },
            shown.map(function (p) {
              var ov = p.overall || {};
              var stt = planningStatus(p, now);
              return h(
                "div",
                {
                  key: p.id,
                  className: "cal-plan-card",
                  onClick: function () { setOpenId(p.id); },
                },
                h(
                  "div",
                  { className: "flex items-start justify-between gap-3" },
                  h(
                    "div",
                    { className: "space-y-0.5" },
                    h("div", { className: "font-semibold text-sm" }, p.name),
                    p.period_label ? h("div", { className: "text-xs opacity-60" }, p.period_label) : null
                  ),
                  h(
                    "div",
                    { className: "flex items-center gap-1 flex-shrink-0" },
                    h(Badge, { variant: "outline" }, stt),
                    h(Badge, { variant: p.report_sent ? "secondary" : "outline" }, p.report_sent ? "report sent" : "pending")
                  )
                ),
                h("div", { className: "mt-2" }, h(ProgressBar, { pct: ov.completion_pct })),
                h(
                  "div",
                  { className: "text-xs opacity-70 mt-1" },
                  (ov.confirmed || 0) + "/" + (ov.total || 0) + " completed (" + (ov.completion_pct || 0) + "%)"
                )
              );
            })
          )
        : null,

      openId ? h(PlanningDetail, { planningId: openId, onClose: function () { setOpenId(null); } }) : null
    );
  }

  // --- app wrapper (tab toggle) --------------------------------------------

  function CalendarHero(props) {
    const view = props.view;
    const setView = props.setView;
    const isPlan = view === "plannings";
    return h(
      "div",
      { className: "cal-hero" },
      h(
        "div",
        { className: "cal-hero-main" },
        h(
          "div",
          { className: "cal-hero-text" },
          h(
            "h1",
            { className: "cal-hero-title" },
            isPlan ? "🗜️ Plannings" : "📅 Calendar"
          ),
          h(
            "p",
            { className: "cal-hero-sub" },
            isPlan
              ? "Objectives & completion for a period."
              : "Your events, reminders, status & timers."
          )
        ),
        h(
          "div",
          { className: "cal-tabs cal-hero-tabs" },
          h(
            "button",
            {
              className: cn("cal-tab", view === "calendar" && "cal-tab-active"),
              onClick: function () { setView("calendar"); },
            },
            "📅 Calendar"
          ),
          h(
            "button",
            {
              className: cn("cal-tab", view === "plannings" && "cal-tab-active"),
              onClick: function () { setView("plannings"); },
            },
            "🗜️ Plannings"
          )
        )
      )
    );
  }

  function CalendarApp() {
    const [view, setView] = useState("calendar");
    return h(
      "div",
      null,
      h(CalendarHero, { view: view, setView: setView }),
      view === "plannings" ? h(PlanningsView, null) : h(CalendarView, null)
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("calendar", CalendarApp);
  }
})();
