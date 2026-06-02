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
    return h(
      "button",
      {
        className: cn("cal-chip", ev.recurring ? "cal-chip-recurring" : "cal-chip-once"),
        title: ev.title + (ev.recurrence_human ? " · " + ev.recurrence_human : ""),
        onClick: function () { props.onOpen(ev.id); },
      },
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
                  const prefix = (ev.has_report ? "📝 " : "") +
                    (ev.all_day ? "All day · " : (fmtTime(ev.occurrence_local, ev.tz) + " · "));
                  return h(
                    "button",
                    {
                      key: ev.id + "@" + ev.occurrence_utc + i,
                      className: cn("cal-chip cal-dayrow", ev.recurring ? "cal-chip-recurring" : "cal-chip-once"),
                      title: ev.title + (ev.recurrence_human ? " · " + ev.recurrence_human : ""),
                      onClick: function () { props.onOpenEvent(ev.id); },
                    },
                    h("span", { className: "cal-chip-title" }, prefix + ev.title)
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

                h("div", { className: "text-xs opacity-40 pt-2 border-t" }, "Read-only — edits are made by talking to Calypso.")
              )
            : null
        )
      )
    );
  }

  // --- main page ------------------------------------------------------------

  function CalendarPage() {
    const [anchor, setAnchor] = useState(function () { return monthAnchor(new Date()); });
    const [openId, setOpenId] = useState(null);
    const [openDay, setOpenDay] = useState(null);
    const { events, loading, error, reload } = useEvents(anchor);

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

    return h(
      "div",
      { className: "p-4 sm:p-6 space-y-4" },
      // header
      h(
        "div",
        { className: "flex items-center justify-between gap-3 flex-wrap" },
        h(
          "div",
          { className: "flex items-center gap-2" },
          h("h1", { className: "text-xl font-semibold" }, "📅 Calendar"),
          h("span", { className: "text-sm opacity-50" }, monthLabel)
        ),
        h(
          "div",
          { className: "flex items-center gap-2" },
          h(Button, { variant: "outline", size: "sm", onClick: function () { setAnchor(new Date(anchor.getFullYear(), anchor.getMonth() - 1, 1)); } }, "◀"),
          h(Button, { variant: "outline", size: "sm", onClick: function () { setAnchor(monthAnchor(new Date())); } }, "Today"),
          h(Button, { variant: "outline", size: "sm", onClick: function () { setAnchor(new Date(anchor.getFullYear(), anchor.getMonth() + 1, 1)); } }, "▶"),
          h(Button, { variant: "ghost", size: "sm", onClick: reload, title: "Refresh" }, "⟳")
        )
      ),

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
        { className: "flex items-center gap-4 text-xs opacity-60" },
        h("span", null, h("span", { className: "cal-chip cal-chip-once", style: { padding: "1px 6px" } }, "one-time")),
        h("span", null, h("span", { className: "cal-chip cal-chip-recurring", style: { padding: "1px 6px" } }, "recurring")),
        h("span", null, "📝 has report")
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

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("calendar", CalendarPage);
  }
})();
