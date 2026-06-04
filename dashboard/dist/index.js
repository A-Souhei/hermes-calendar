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

  // --- status helpers -------------------------------------------------------

  var STATUS_GLYPH = { confirmed: "✓", active: "●", missed: "✗" };

  // Effective display status (backend-derived): a past, still-floating
  // occurrence reads as "missed" while staying floating underneath.
  function effStatus(ev) {
    return ev.effective_status || ev.status || "floating";
  }
  // True when "missed" is only inferred from the past date, not explicitly set.
  function isDerivedMiss(ev) {
    return effStatus(ev) === "missed" && (ev.status === "floating" || !ev.status);
  }
  // A coloured status pill for an event (confirmed / running / missed / upcoming).
  function statusBadge(ev) {
    var eff = effStatus(ev); // confirmed | missed | active | floating
    var derived = isDerivedMiss(ev);
    var label = eff === "floating" ? "upcoming" : (eff === "active" ? "running" : eff);
    var glyph = STATUS_GLYPH[eff] || "";
    return h(
      "span",
      { className: cn("cal-status", "cal-status-" + eff, derived && "cal-status-derived") },
      (glyph ? glyph + " " : "") + label
    );
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

  // Live H:MM:SS clock since startedUtc (ticks visibly each second, unlike
  // fmtDuration which collapses seconds past the first hour).
  function fmtClock(startedUtc) {
    if (!startedUtc) return null;
    try {
      var sec = Math.max(0, Math.floor((Date.now() - new Date(startedUtc).getTime()) / 1000));
      var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
      return h + ":" + (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
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

  function useUsers() {
    const [users, setUsers] = useState([]);
    useEffect(function () {
      SDK.fetchJSON(API + "/users")
        .then(function (data) { setUsers((data && data.users) || []); })
        .catch(function () { setUsers([]); });
    }, []);
    return users;
  }

  function useCategories(owner) {
    const [categories, setCategories] = useState([]);
    useEffect(function () {
      var url = API + "/categories";
      if (owner) url += "?owner=" + encodeURIComponent(owner);
      SDK.fetchJSON(url)
        .then(function (data) { setCategories((data && data.categories) || []); })
        .catch(function () { setCategories([]); });
    }, [owner]);
    return categories;
  }

  // Currently-running timers (optionally scoped to the selected owner). Polled
  // every 10s so the banner appears/disappears as sessions start/stop, while
  // the elapsed time itself ticks client-side from started_utc.
  function useTimers(owner) {
    const [timers, setTimers] = useState([]);
    useEffect(function () {
      let alive = true;
      function load() {
        var url = API + "/timers";
        if (owner) url += "?owner=" + encodeURIComponent(owner);
        SDK.fetchJSON(url)
          .then(function (d) { if (alive) setTimers((d && d.timers) || []); })
          .catch(function () { if (alive) setTimers([]); });
      }
      load();
      var iv = setInterval(load, 10000);
      return function () { alive = false; clearInterval(iv); };
    }, [owner]);
    return timers;
  }

  function useEvents(anchor, owner, category) {
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
      var url = API + "/events?from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to);
      if (owner) url += "&owner=" + encodeURIComponent(owner);
      if (category) url += "&category=" + encodeURIComponent(category);
      SDK.fetchJSON(url)
        .then(function (data) {
          setEvents((data && data.events) || []);
          setLoading(false);
        })
        .catch(function (err) {
          setError((err && err.message) || "Failed to load events");
          setLoading(false);
        });
    }, [anchor, owner, category]);

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
    const [resuming, setResuming] = useState(false);
    const [stopping, setStopping] = useState(false);
    const [confirming, setConfirming] = useState(false);
    const [cancelling, setCancelling] = useState(false);
    const [report, setReport] = useState("");
    const [actionMsg, setActionMsg] = useState(null);
    const [reloadTick, setReloadTick] = useState(0);
    const [, setNowTick] = useState(0);

    // Clear any action message only when switching to a different event (not on
    // a reload, so a "Stopped"/"Started" confirmation survives the refetch).
    useEffect(function () { setActionMsg(null); setReport(""); }, [id]);

    useEffect(function () {
      let alive = true;
      setLoading(true);
      setError(null);
      SDK.fetchJSON(API + "/event/" + encodeURIComponent(id))
        .then(function (d) { if (alive) { setData(d); setLoading(false); } })
        .catch(function (err) { if (alive) { setError((err && err.message) || "Failed to load"); setLoading(false); } });
      return function () { alive = false; };
    }, [id, reloadTick]);

    // Prefill the report textarea from any existing report notes for targetOcc.
    useEffect(function () {
      if (!data) return;
      var targetOcc = (props.occurrence || data.start_utc || "");
      var existing = (data.reports || []).filter(function (r) { return r.occurrence_utc === targetOcc; })[0];
      var notes = (existing && existing.report && existing.report.notes) || "";
      setReport(notes);
    }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

    // While a session is running, re-render every second so the elapsed time
    // shown in the details ticks live. No interval is set up when idle.
    useEffect(function () {
      var active = data && data.statuses && data.statuses.some(function (s) {
        return s.status === "active" && s.started_utc;
      });
      if (!active) return undefined;
      var iv = setInterval(function () { setNowTick(function (t) { return t + 1; }); }, 1000);
      return function () { clearInterval(iv); };
    }, [data]);

    function handleResume() {
      if (!data || !data.job) return;
      setResuming(true);
      setActionMsg(null);
      SDK.fetchJSON(API + "/jobs/resume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner: data.owner, job: data.job }),
      })
        .then(function (result) {
          var msg = "▶ Started a new session";
          if (result && result.warning) msg += " — " + result.warning;
          setActionMsg({ ok: true, text: msg });
          setResuming(false);
          if (props.onResumed) props.onResumed();
        })
        .catch(function (err) {
          setActionMsg({ ok: false, text: (err && err.message) || "Resume failed" });
          setResuming(false);
        });
    }

    function handleStop() {
      if (!data || !data.id) return;
      setStopping(true);
      setActionMsg(null);
      SDK.fetchJSON(API + "/jobs/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_id: data.id }),
      })
        .then(function (result) {
          var d = result && result.duration_seconds;
          var msg = "■ Stopped" + (d != null ? " — logged " + (fmtDuration(d) || "0s") : "");
          setActionMsg({ ok: true, text: msg });
          setStopping(false);
          setReloadTick(function (t) { return t + 1; });  // refetch: flips this event to confirmed
          if (props.onResumed) props.onResumed();          // refresh the calendar grid
        })
        .catch(function (err) {
          setActionMsg({ ok: false, text: (err && err.message) || "Stop failed" });
          setStopping(false);
        });
    }

    function handleConfirm() {
      if (!data || !data.id) return;
      var targetOcc = (props.occurrence || data.start_utc || "");
      setConfirming(true);
      setActionMsg(null);
      SDK.fetchJSON(API + "/event/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_id: data.id, occurrence_utc: targetOcc, report: report }),
      })
        .then(function (result) {
          var msg = "✓ Confirmed" + (result && result.report_saved ? " — report saved" : "");
          setActionMsg({ ok: true, text: msg });
          setConfirming(false);
          setReloadTick(function (t) { return t + 1; });
          if (props.onResumed) props.onResumed();
        })
        .catch(function (err) {
          setActionMsg({ ok: false, text: (err && err.message) || "Confirm failed" });
          setConfirming(false);
        });
    }

    function handleCancel() {
      if (!data || !data.id) return;
      var targetOcc = (props.occurrence || data.start_utc || "");
      setCancelling(true);
      setActionMsg(null);
      SDK.fetchJSON(API + "/event/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_id: data.id, occurrence_utc: targetOcc, report: report }),
      })
        .then(function (result) {
          var msg = "✕ Cancelled" + (result && result.report_saved ? " — reason saved" : "");
          setActionMsg({ ok: true, text: msg });
          setCancelling(false);
          setReloadTick(function (t) { return t + 1; });
          if (props.onResumed) props.onResumed();
        })
        .catch(function (err) {
          setActionMsg({ ok: false, text: (err && err.message) || "Cancel failed" });
          setCancelling(false);
        });
    }

    const meeting = data && data.meeting;
    const tags = (data && data.tags) || [];
    const activeRow = ((data && data.statuses) || []).filter(function (s) {
      return s.status === "active" && s.started_utc;
    })[0];
    const activeStarted = activeRow ? activeRow.started_utc : null;
    const isRegularEvent = data && data.kind !== "note" && !data.job;
    var targetOcc = data ? (props.occurrence || data.start_utc || "") : "";
    // The stored status row for this occurrence (if any) + when it was set —
    // used to show the confirmation/cancellation time and block re-applying it.
    var statusRow = (isRegularEvent && data)
      ? (data.statuses || []).filter(function (s) { return s.occurrence_utc === targetOcc; })[0]
      : null;
    var confirmedAt = (statusRow && statusRow.status === "confirmed")
      ? (statusRow.updated_utc || statusRow.created_utc) : null;
    var cancelledAt = (statusRow && statusRow.status === "missed")
      ? (statusRow.updated_utc || statusRow.created_utc) : null;
    var alreadyConfirmed = !!confirmedAt;
    var alreadyCancelled = !!cancelledAt;
    // A future occurrence can't be confirmed (it hasn't happened) — it can be
    // cancelled instead (marked 'missed'). Past/now → Confirm; future → Cancel.
    var isFuture = targetOcc ? (new Date(targetOcc).getTime() > Date.now()) : false;

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
              h("h2", { className: "text-lg font-semibold leading-tight" },
                data && data.number != null
                  ? h("span", null,
                      h("span", { className: "cal-evnum" }, "#" + data.number + " "),
                      data.title)
                  : (data ? data.title : "Loading…")),
              data && data.recurrence_human
                ? h(Badge, { variant: "secondary" }, "↻ " + data.recurrence_human)
                : null,
              activeStarted
                ? h("span", { className: "cal-running-live" }, "● Running " + (fmtClock(activeStarted) || "0:00:00"))
                : null
            ),
            h(
              "div",
              { className: "flex items-center gap-2" },
              // A running session shows Stop; a stopped job shows Resume.
              activeStarted
                ? h(Button, {
                    variant: "destructive",
                    size: "sm",
                    onClick: handleStop,
                    disabled: stopping,
                    title: "Stop the running session and log its duration",
                  }, stopping ? "…" : "■ Stop")
                : (data && data.job
                    ? h(Button, {
                        variant: "outline",
                        size: "sm",
                        onClick: handleResume,
                        disabled: resuming,
                        title: "Resume this job (start a new session)",
                      }, resuming ? "…" : "▶ Resume job")
                    : null),
              // Regular (green) events: kind === "event" and no job.
              // Past/now → Confirm (it happened); future → Cancel (call it off).
              isRegularEvent
                ? (isFuture
                    ? (alreadyCancelled
                        ? null  // already cancelled — no re-cancel (status shown below)
                        : h("button", {
                            className: "cal-cancel-btn",
                            onClick: handleCancel,
                            disabled: cancelling,
                            title: "Cancel this upcoming occurrence",
                          }, cancelling ? "…" : "✕ Cancel"))
                    : (alreadyConfirmed
                        ? null  // already confirmed — no re-confirm (status shown below)
                        : h("button", {
                            className: "cal-confirm-btn",
                            onClick: handleConfirm,
                            disabled: confirming,
                            title: "Mark this occurrence confirmed",
                          }, confirming ? "…" : "✓ Confirm")))
                : null,
              h(Button, { variant: "ghost", size: "sm", onClick: props.onClose }, "✕")
            )
          ),
          actionMsg
            ? h("div", {
                className: "text-sm " + (actionMsg.ok ? "cal-resume-ok" : "text-red-600"),
              }, actionMsg.text)
            : null,

          loading ? h("div", { className: "text-sm opacity-60 py-6" }, "Loading…") : null,
          error ? h("div", { className: "text-sm text-red-600 py-6" }, error) : null,

          data
            ? h(
                "div",
                { className: "space-y-4" },
                h(
                  "dl",
                  { className: "cal-kv" },
                  h(KV, { label: "When", value: (data.end_utc && !data.all_day)
                    ? fmtDateTime(data.start_utc, data.tz, data.all_day) + " – " + fmtTime(data.end_utc, data.tz)
                    : fmtDateTime(data.start_utc, data.tz, data.all_day) + (data.all_day ? " (all day)" : "") }),
                  data.planning
                    ? h(KV, { label: "Planning", value: h(Badge, { variant: "secondary" }, "🗜️ " + data.planning) })
                    : null,
                  data.job
                    ? h(KV, { label: "Job", value: data.job })
                    : null,
                  data.category
                    ? h(KV, { label: "Category", value: data.category })
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
                        var running = s.status === "active" && s.started_utc ? fmtClock(s.started_utc) : null;
                        return h(
                          "div",
                          { key: s.occurrence_utc, className: "rounded-md border p-3 space-y-1 text-sm" },
                          h(
                            "div",
                            { className: "flex items-center justify-between gap-2 flex-wrap" },
                            h("span", { className: "font-medium" }, fmtDateTime(s.occurrence_local || s.occurrence_utc, data.tz)),
                            // Job events are inherently confirmed (timer sessions) — the
                            // 'confirmed' status is a planning concept, so don't surface it
                            // here for jobs. (A live 'active' session is still shown.)
                            (data.job && s.status === "confirmed")
                              ? null
                              : h("span", { className: "cal-status cal-status-" + s.status }, (STATUS_GLYPH[s.status] || "") + " " + s.status)
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

                // Confirm/Cancel UI — regular events only. Already-confirmed /
                // already-cancelled occurrences show the timestamp (no re-apply);
                // otherwise a textarea: future → cancel reason, past/now → report.
                isRegularEvent
                  ? (alreadyConfirmed
                      ? h(
                          "div",
                          { className: "space-y-1 pt-2 border-t" },
                          h("div", { className: "text-sm font-medium cal-resume-ok" }, "✓ Confirmed"),
                          h("div", { className: "text-xs opacity-60" }, "on " + fmtDateTime(confirmedAt, data.tz))
                        )
                      : alreadyCancelled
                        ? h(
                            "div",
                            { className: "space-y-1 pt-2 border-t" },
                            h("div", { className: "text-sm font-medium" }, "✕ Cancelled"),
                            h("div", { className: "text-xs opacity-60" }, "on " + fmtDateTime(cancelledAt, data.tz))
                          )
                        : h(
                            "div",
                            { className: "space-y-2 pt-2 border-t" },
                            h("label", { className: "text-xs font-semibold uppercase tracking-wide opacity-60", htmlFor: "cal-report-input" },
                              isFuture ? "Reason for cancellation (optional)" : "Activity report / transcription (optional)"),
                            h("textarea", {
                              id: "cal-report-input",
                              className: "cal-report-textarea",
                              value: report,
                              onChange: function (e) { setReport(e.target.value); },
                              placeholder: isFuture
                                ? "Why is this being cancelled?…"
                                : "Add notes or a transcription for this occurrence…",
                            })
                          ))
                  : h("div", { className: "text-xs opacity-40 pt-2 border-t" }, "Read-only — edits are made by talking to the assistant.")
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

  // Live "running now" banner: names the currently-running session(s) with a
  // ticking elapsed clock. Owns its own 1s tick so only this re-renders.
  function RunningBanner(props) {
    const timers = props.timers || [];
    const [, setTick] = useState(0);
    useEffect(function () {
      if (!timers.length) return undefined;
      var iv = setInterval(function () { setTick(function (t) { return t + 1; }); }, 1000);
      return function () { clearInterval(iv); };
    }, [timers.length]);
    if (!timers.length) return null;
    return h(
      "div",
      { className: "cal-running-banner-row" },
      timers.map(function (t, i) {
        var label = t.title + (t.job ? " · " + t.job : "");
        return h(
          "button",
          {
            key: t.event_id + i,
            className: "cal-running-banner",
            title: "Open the running session",
            onClick: function () { if (props.onOpen) props.onOpen(t.event_id, t.occurrence_utc || null); },
          },
          h("span", { className: "cal-running-dot" }, "●"),
          h("span", { className: "cal-running-label" }, "Running"),
          h("span", { className: "cal-running-name" }, label),
          h("span", { className: "cal-running-clock" }, fmtClock(t.started_utc) || "0:00:00")
        );
      })
    );
  }

  // --- compact month picker -------------------------------------------------
  // Small calendar: day numbers only. Today keeps its filled marker; days with
  // events are signalled by a dot + accent number text (never overrides today).
  function SmallCalendar(props) {
    const cells = props.cells || [];
    const selectedKey = props.selectedKey;
    return h(
      "div",
      { className: cn("sc", props.loading && "opacity-50") },
      h(
        "div",
        { className: "sc-weekrow" },
        WEEKDAYS.map(function (d) {
          return h("div", { key: d, className: "sc-weekday" }, d.charAt(0));
        })
      ),
      h(
        "div",
        { className: "sc-grid" },
        cells.map(function (c) {
          var hasEvents = c.events && c.events.length > 0;
          var hasNote = c.events && c.events.some(function (e) { return e.kind === "note"; });
          return h(
            "button",
            {
              key: c.key,
              className: cn(
                "sc-day",
                !c.inMonth && "sc-day-out",
                hasEvents && "sc-day-has",
                hasNote && "sc-day-note",
                c.key === selectedKey && "sc-day-selected",
                c.isToday && "sc-day-today"
              ),
              title: hasEvents
                ? c.events.length + " event" + (c.events.length > 1 ? "s" : "")
                : undefined,
              onClick: function () { props.onSelectDate(c.date); },
            },
            h("span", { className: "sc-day-num" }, String(c.date.getDate()))
          );
        })
      )
    );
  }

  // --- agenda panel (events for the selected day) ---------------------------
  function AgendaPanel(props) {
    const date = props.date;
    const events = props.events || [];
    const dateLabel = date.toLocaleDateString(undefined, {
      weekday: "long", month: "long", day: "numeric", year: "numeric",
    });
    return h(
      "div",
      { className: "agenda" },
      h(
        "div",
        { className: "agenda-head" },
        h("div", { className: "agenda-title" }, dateLabel),
        h(
          "div",
          { className: "agenda-count" },
          events.length ? events.length + " event" + (events.length > 1 ? "s" : "") : "No events"
        )
      ),
      events.length
        ? h(
            "div",
            { className: "agenda-list" },
            events.map(function (ev, i) {
              var startTime = ev.all_day ? null : fmtTime(ev.occurrence_local || ev.occurrence_utc, ev.tz);
              var timeStr;
              if (ev.all_day) {
                timeStr = "All day";
              } else if (ev.end_utc) {
                timeStr = startTime + " – " + fmtTime(ev.end_utc, ev.tz);
              } else {
                timeStr = startTime;
              }
              var dur = ev.duration_seconds != null ? fmtDuration(ev.duration_seconds) : null;
              return h(
                "button",
                {
                  key: ev.id + "@" + ev.occurrence_utc + i,
                  className: cn("agenda-row", ev.job && "agenda-row-job", ev.kind === "note" && "agenda-row-note"),
                  onClick: function () { props.onOpen(ev.id, ev.occurrence_utc); },
                },
                h("span", { className: "agenda-time" }, timeStr),
                h(
                  "span",
                  { className: "agenda-body" },
                  h(
                    "span",
                    { className: "agenda-titleline" },
                    ev.kind === "note" ? h("span", { className: "agenda-note-glyph" }, "🗒️") : null,
                    ev.planning ? h("span", { className: "cal-plan-glyph" }, "🗜️") : null,
                    ev.number != null ? h("span", { className: "agenda-evnum" }, "#" + ev.number + " ") : null,
                    h("span", { className: "agenda-evtitle" }, (ev.has_report ? "📝 " : "") + ev.title),
                    dur ? h("span", { className: "agenda-dur" }, dur) : null
                  ),
                  // status + category + job + location badges (for every event)
                  h(
                    "span",
                    { className: "agenda-sub" },
                    // Notes have no status; job events are inherently confirmed
                    // (timer sessions) so don't show a 'confirmed' badge for them
                    // — a live 'running' session still shows.
                    (ev.kind === "note" || (ev.job && effStatus(ev) === "confirmed")) ? null : statusBadge(ev),
                    ev.category ? h("span", { className: "agenda-cat" }, ev.category) : null,
                    ev.job ? h("span", { className: "agenda-job" }, "▸ " + ev.job) : null,
                    ev.location ? h("span", { className: "agenda-loc" }, "📍 " + ev.location) : null
                  )
                )
              );
            })
          )
        : h("div", { className: "agenda-empty" }, "Nothing scheduled for this day.")
    );
  }

  // --- month calendar view --------------------------------------------------

  function CalendarView(props) {
    const owner = props.owner || null;
    const category = props.category || null;
    const timers = useTimers(owner);
    const [anchor, setAnchor] = useState(function () { return monthAnchor(new Date()); });
    const [selectedDay, setSelectedDay] = useState(function () { return new Date(); });
    const [openModal, setOpenModal] = useState(null); // {id, occ}
    const { events, loading, error, reload } = useEvents(anchor, owner, category);

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
        if (ev.kind === "note") return;
        s.total++;
        if (ev.status === "confirmed") s.confirmed++;
        if (ev.status === "active") s.active++;
        var eff = effStatus(ev);
        if (eff === "missed") s.missed++;
        if (eff === "floating") s.upcoming++;
      });
      return s;
    }, [events]);

    // Events for the selected day, sorted by time (drives the agenda panel).
    const selectedKey = isoDate(selectedDay);
    const selectedEvents = useMemo(function () {
      return (byDate[selectedKey] || []).slice().sort(function (a, b) {
        return new Date(a.occurrence_utc || a.occurrence_local || 0) -
               new Date(b.occurrence_utc || b.occurrence_local || 0);
      });
    }, [byDate, selectedKey]);

    // Month nav also moves the selection so the agenda follows the visible month.
    function goMonth(delta) {
      var d = new Date(anchor.getFullYear(), anchor.getMonth() + delta, 1);
      setAnchor(d);
      setSelectedDay(d);
    }
    function goToday() {
      var now = new Date();
      setAnchor(monthAnchor(now));
      setSelectedDay(now);
    }

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
          h(Button, { variant: "outline", size: "sm", onClick: function () { goMonth(-1); } }, "◀"),
          h(Button, { variant: "outline", size: "sm", onClick: goToday }, "Today"),
          h(Button, { variant: "outline", size: "sm", onClick: function () { goMonth(1); } }, "▶"),
          h(Button, { variant: "ghost", size: "sm", onClick: reload, title: "Refresh" }, "⟳")
        )
      ),

      // currently-running session(s)
      h(RunningBanner, { timers: timers, onOpen: function (id, occ) { setOpenModal({ id: id, occ: occ || null }); } }),

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

      error ? h("div", { className: "text-sm text-red-600" }, "⚠ " + error) : null,

      // split: small calendar (left) + agenda for the selected day (right)
      h(
        "div",
        { className: "cal-split" },
        h(
          Card,
          null,
          h(
            CardContent,
            { className: "p-3" },
            h(SmallCalendar, {
              cells: cells,
              selectedKey: selectedKey,
              loading: loading,
              onSelectDate: function (d) { setSelectedDay(d); },
            })
          )
        ),
        h(
          Card,
          null,
          h(
            CardContent,
            { className: "p-0" },
            h(AgendaPanel, {
              date: selectedDay,
              events: selectedEvents,
              onOpen: function (id, occ) { setOpenModal({ id: id, occ: occ || null }); },
            })
          )
        )
      ),

      openModal ? h(DetailModal, {
        eventId: openModal.id,
        occurrence: openModal.occ,
        onClose: function () { setOpenModal(null); },
        onResumed: reload,
      }) : null
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

  function PlanningsView(props) {
    const owner = props.owner || null;
    const [plannings, setPlannings] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [openId, setOpenId] = useState(null);
    const [filter, setFilter] = useState("all");

    const load = useCallback(function () {
      setLoading(true);
      setError(null);
      var url = API + "/plannings";
      if (owner) url += "?owner=" + encodeURIComponent(owner);
      SDK.fetchJSON(url)
        .then(function (data) {
          setPlannings((data && data.plannings) || []);
          setLoading(false);
        })
        .catch(function (err) {
          setError((err && err.message) || "Failed to load plannings");
          setLoading(false);
        });
    }, [owner]);

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
    const users = props.users || [];
    const categories = props.categories || [];
    const selectedOwner = props.selectedOwner;
    const setSelectedOwner = props.setSelectedOwner;
    const selectedCategory = props.selectedCategory;
    const setSelectedCategory = props.setSelectedCategory;
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
          { className: "flex items-center gap-3 flex-wrap" },
          users.length > 0
            ? h(
                "select",
                {
                  className: "cal-user-select",
                  value: selectedOwner || "",
                  onChange: function (e) { setSelectedOwner(e.target.value || null); },
                  title: "Filter by user",
                },
                h("option", { value: "" }, "All users"),
                users.map(function (u) {
                  return h("option", { key: u, value: u }, u);
                })
              )
            : null,
          categories.length > 0
            ? h(
                "select",
                {
                  className: "cal-user-select",
                  value: selectedCategory || "",
                  onChange: function (e) { setSelectedCategory(e.target.value || null); },
                  title: "Filter by category",
                },
                h("option", { value: "" }, "All categories"),
                categories.map(function (c) {
                  return h("option", { key: c, value: c }, c);
                })
              )
            : null,
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
      )
    );
  }

  function CalendarApp() {
    const [view, setView] = useState("calendar");
    const [selectedOwner, setSelectedOwner] = useState(null);
    const [selectedCategory, setSelectedCategory] = useState(null);
    const users = useUsers();
    const categories = useCategories(selectedOwner);
    return h(
      "div",
      null,
      h(CalendarHero, {
        view: view,
        setView: setView,
        users: users,
        categories: categories,
        selectedOwner: selectedOwner,
        setSelectedOwner: setSelectedOwner,
        selectedCategory: selectedCategory,
        setSelectedCategory: setSelectedCategory,
      }),
      view === "plannings"
        ? h(PlanningsView, { owner: selectedOwner })
        : h(CalendarView, { owner: selectedOwner, category: selectedCategory })
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("calendar", CalendarApp);
  }
})();
