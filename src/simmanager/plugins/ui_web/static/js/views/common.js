// Pieces shared by several views.

import { api } from "../api.js";
import * as ws from "../ws.js";
import { h, clear, fmtTime, fmtDuration, fmtRough, pct, progressBar, stateBadge } from "../util.js";
import { openTerminal } from "../terminal/open.js";

export function clusterCard(c, { onReconnect } = {}) {
  const poll = c.last_poll;
  const button = h("button", {
    onclick: async () => {
      const open = (c.terminals && c.terminals.open) || 0;
      if (open && !confirm(`Reconnecting ${c.id} closes its SSH connection: ${open} open terminal(s) will end. Continue?`)) return;
      button.disabled = true;
      try { await api.reconnect(c.id); if (onReconnect) onReconnect(); }
      catch (err) { alert(err.message); }
      finally { button.disabled = false; }
    },
  }, "Reconnect");
  return h("div", { class: "cluster" },
    h("div", { class: "head" }, h("strong", {}, c.id), stateBadge(c.state), terminalPicker(c), button),
    h("div", { class: "small muted mono" }, c.target, " · ", c.scheduler),
    c.detail ? h("div", { class: "small" }, c.detail) : null,
    poll
      ? h("div", { class: "small" },
          poll.ok ? `${poll.active_jobs} active job(s)` : h("span", { class: "error" }, poll.error || "poll failed"),
          ` · polled ${fmtTime(poll.at)}`,
          poll.next_poll_s ? ` · next in ${Math.round(poll.next_poll_s)} s` : "")
      : h("div", { class: "small muted" }, "not polled yet"));
}

/** "Terminal" drop-down of a cluster card: home folder or one of the work roots. */
export function terminalPicker(c) {
  const t = c.terminals || {};
  if (!t.available) return null;
  const select = h("select", {
    class: "terminal-picker",
    title: `${t.open || 0} of ${t.max} terminals open on ${c.id}`,
    onchange: () => {
      const cwd = select.value;
      select.selectedIndex = 0;
      openTerminal({ cluster: c.id, cwd: cwd === "~" ? null : cwd });
    },
  },
  h("option", { value: "" }, `Terminal${t.open ? ` (${t.open} open)` : ""}…`),
  h("option", { value: "~" }, "home folder"),
  (c.work_roots || []).map((root) => h("option", { value: root }, root)));
  return select;
}

export function runRow(run, { selectable = false, selected = null } = {}) {
  const job = run.job;
  const solver = run.solver;
  const progress = solver ? solver.progress : null;
  const checkbox = selectable
    ? h("input", { type: "checkbox", onclick: (e) => e.stopPropagation(), onchange: (e) => selected(run.id, e.target.checked) })
    : null;
  const tr = h("tr", { class: "clickable", onclick: () => { location.hash = `#/runs/${run.id}`; } },
    selectable ? h("td", {}, checkbox) : null,
    h("td", {}, `#${run.id}`),
    h("td", {}, run.cluster_id || "—"),
    h("td", { class: "mono" }, job ? job.job_id : "manual"),
    h("td", { class: "wrap" }, job ? job.name : (run.work_dir || "")),
    h("td", {}, job ? stateBadge(job.state) : "—"),
    h("td", { class: "mono small" }, job ? `${fmtDuration(job.elapsed_s)} / ${job.time_limit_s ? fmtDuration(job.time_limit_s) : "∞"}` : ""),
    h("td", { class: "progress" }, progress === null ? "—" : [progressBar(progress), " ", pct(progress)]),
    h("td", {}, run.eta ? h("span", { title: `ends ~${fmtTime(run.eta.at)}` }, fmtRough(run.eta.remaining_s)) : "—"),
    h("td", {}, solver ? [stateBadge(solver.state), solver.warning_count ? h("span", { class: "badge warn" }, `⚠ ${solver.warning_count}`) : null] : "—"),
  );
  tr.dataset.runId = run.id;
  return tr;
}

export function runsTable(runs, options = {}) {
  return h("table", {},
    h("thead", {}, h("tr", {},
      options.selectable ? h("th", {}, "") : null,
      ["run", "cluster", "job", "name / directory", "job state", "elapsed / limit", "progress", "ETA", "solver"]
        .map((t) => h("th", {}, t)))),
    h("tbody", {}, runs.map((r) => runRow(r, options))));
}

export function describeEvent(e) {
  const p = e.payload || {};
  switch (e.kind) {
    case "job.state_changed": return `job ${p.job_id || ""} ${p.from || ""} → ${p.to || ""}`;
    case "job.discovered": return `job ${p.job_id || ""} discovered (${p.name || ""})`;
    case "run.solver_state_changed": return `solver ${p.from} → ${p.to}${p.error_message ? `: ${p.error_message}` : ""}`;
    case "run.created": return `run created (${p.origin || "discovered"})`;
    default: {
      const detail = p.detail || p.message || p.error || "";
      return detail ? String(detail) : "";
    }
  }
}

export function eventItem(e) {
  return h("li", {},
    h("span", { class: "muted small mono" }, fmtTime(e.ts)), " ",
    e.cluster_id ? h("strong", {}, e.cluster_id) : null, " ",
    e.run_id ? h("a", { href: `#/runs/${e.run_id}` }, `#${e.run_id}`) : null, " ",
    h("span", { class: "badge" }, e.kind), " ",
    describeEvent(e));
}

/** Subscribe to WebSocket types for the lifetime of a view; returns the teardown. */
export function listen(map) {
  const offs = Object.entries(map).map(([type, fn]) => ws.on(type, fn));
  return () => offs.forEach((off) => off());
}

export { clear };
