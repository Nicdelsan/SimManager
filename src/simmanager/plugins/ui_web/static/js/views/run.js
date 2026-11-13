// Run detail: header (job, solver, progress, ETA), files/backfill, series list and plots
// (one per unit and x axis), with live points over the WebSocket.

import { api } from "../api.js";
import * as ws from "../ws.js";
import { Plot, isResidual } from "../plot.js";
import { h, clear, debounce, basename, fmtDuration, fmtRough, fmtTime, pct, progressBar, stateBadge } from "../util.js";
import { eventItem, listen } from "./common.js";
import { openTerminal } from "../terminal/open.js";

const BACKFILL_RELOAD_MS = 10000;

function storageKey(runId) { return `simman.run.${runId}.series`; }

function loadSelection(runId) {
  try { return JSON.parse(localStorage.getItem(storageKey(runId)) || "null"); } catch { return null; }
}

function saveSelection(runId, ids) {
  try { localStorage.setItem(storageKey(runId), JSON.stringify([...ids])); } catch { /* private mode */ }
}

export function seriesLabel(s) {
  return s.qualifier ? `${s.name} [${s.qualifier}]` : s.name;
}

export function groupKey(s) {
  return `${s.unit || "—"}|${s.x_name}|${s.x_unit}`;
}

export function groupTitle(s) {
  const y = s.unit ? s.unit : "no unit";
  const x = s.x_unit ? `${s.x_name} (${s.x_unit})` : s.x_name;
  return `${y} vs ${x}`;
}

function header(detail) {
  const job = detail.job;
  const solver = detail.solver;
  return h("div", {},
    h("h1", {}, `Run #${detail.id}`, " ",
      h("span", { class: "muted" }, detail.cluster_id || ""), " ",
      job ? h("span", {}, `· job ${job.job_id} ${job.name}`) : h("span", { class: "muted" }, "· manual"), " ",
      job ? stateBadge(job.state) : null),
    h("div", { class: "row small" },
      h("span", { class: "mono" }, detail.work_dir || ""),
      solver ? h("span", {}, solver.solver, " ", stateBadge(solver.state)) : h("span", { class: "muted" }, "solver not detected yet"),
      solver && solver.progress !== null ? h("span", {}, progressBar(solver.progress), " ", pct(solver.progress)) : null,
      detail.eta ? h("span", {}, `ETA ${fmtRough(detail.eta.remaining_s)} (≈ ${fmtTime(detail.eta.at)})`) : null,
      job ? h("span", { class: "mono" }, `elapsed ${fmtDuration(job.elapsed_s)} / ${job.time_limit_s ? fmtDuration(job.time_limit_s) : "∞"}`) : null,
      solver && solver.warning_count ? h("span", { class: "badge warn" }, `${solver.warning_count} warnings`) : null,
      h("a", { href: `#/compare?runs=${detail.id}` }, "Compare…"),
      detail.cluster_id && detail.work_dir
        ? h("button", { type: "button", title: "Open a terminal in the run folder",
            onclick: () => openTerminal({ cluster: detail.cluster_id, cwd: detail.work_dir, run: detail.id }) },
          "Terminal")
        : null),
    solver && solver.error_message ? h("p", { class: "error" }, solver.error_message) : null);
}

function filesLine(files) {
  if (!files.length) return h("span", { class: "muted small" }, "no files read yet");
  return h("div", { class: "small" }, files.map((f) =>
    h("span", { class: "badge", title: f.file_path },
      basename(f.file_path), " ",
      f.backfill_pending ? `◐ ${pct(f.read_fraction)} read` : "✓")).flatMap((b) => [b, " "]));
}

export async function runView(main, { params }) {
  const runId = Number(params[0]);
  let detail = await api.run(runId);
  const headerEl = h("div", { class: "panel" });
  const filesEl = h("div", {});
  const listEl = h("div", { class: "series-list panel" });
  const plotsEl = h("div", { class: "grow panel" });
  const eventsEl = h("ul", { class: "events" });
  clear(main,
    headerEl,
    h("div", { class: "row" }, listEl, plotsEl),
    h("div", { class: "panel" }, h("h2", {}, "Events"), eventsEl));

  let plots = [];
  let selected = new Set(loadSelection(runId) || []);
  let backfillTimer = null;

  const progressSeries = () => detail.series.filter((s) => s.progress);
  const dataSeries = () => detail.series.filter((s) => !s.progress);

  if (!selected.size) {
    const first = dataSeries()[0];
    if (first) {
      dataSeries().filter((s) => s.file_path === first.file_path).slice(0, 3).forEach((s) => selected.add(s.id));
    }
  }

  const renderHeader = () => {
    clear(headerEl, header(detail));
    clear(filesEl, filesLine(detail.files));
    headerEl.append(filesEl);
  };

  const renderList = () => {
    const byFile = new Map();
    for (const s of dataSeries()) {
      if (!byFile.has(s.file_path)) byFile.set(s.file_path, []);
      byFile.get(s.file_path).push(s);
    }
    clear(listEl, h("h2", {}, "Series"),
      byFile.size ? null : h("p", { class: "muted small" }, "No data yet."),
      [...byFile.entries()].map(([file, list]) =>
        h("details", { open: list.some((s) => selected.has(s.id)) },
          h("summary", { title: file }, basename(file), h("span", { class: "muted small" }, ` (${list.length})`)),
          list.map((s) => h("label", { title: `${s.point_count} points` },
            h("input", {
              type: "checkbox",
              checked: selected.has(s.id),
              onchange: (e) => {
                if (e.target.checked) selected.add(s.id); else selected.delete(s.id);
                saveSelection(runId, selected);
                buildPlots();
              },
            }), " ", seriesLabel(s), s.unit ? h("span", { class: "muted small" }, ` ${s.unit}`) : null)))));
  };

  const subscribe = () => {
    const series = plots.flatMap((p) => p.seriesIds().map((id) => ({ id, last_x: p.tailX(id) })));
    ws.send({ type: "subscribe", run_id: runId, series });
  };

  const buildPlots = async () => {
    plots.forEach((p) => p.destroy());
    plots = [];
    clear(plotsEl);
    const progress = progressSeries();
    if (progress.length) {
      plots.push(new Plot(plotsEl, {
        title: "Progress", time: true, percent: true, xLabel: "time",
        series: progress.map((s) => ({ meta: s, label: "progress" })),
      }));
    }
    const groups = new Map();
    for (const s of dataSeries().filter((t) => selected.has(t.id))) {
      const key = groupKey(s);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(s);
    }
    for (const list of groups.values()) {
      plots.push(new Plot(plotsEl, {
        title: groupTitle(list[0]),
        xLabel: list[0].x_name,
        log: list.every(isResidual),
        series: list.map((s) => ({ meta: s, label: `${basename(s.file_path)}: ${seriesLabel(s)}` })),
      }));
    }
    if (!plots.length) plotsEl.append(h("p", { class: "muted" }, "Select series on the left."));
    await Promise.all(plots.map((p) => p.load()));
    subscribe();
  };

  const refreshDetail = debounce(async () => {
    const known = new Set(detail.series.map((s) => s.id));
    detail = await api.run(runId);
    renderHeader();
    if (detail.series.some((s) => !known.has(s.id))) {
      renderList();
      if (progressSeries().some((s) => !known.has(s.id))) buildPlots();
    }
  }, 2000);

  const reloadAll = () => {
    Promise.all(plots.map((p) => p.load())).then(subscribe).catch((err) => console.error(err));
  };

  renderHeader();
  renderList();
  await buildPlots();
  api.events({ run_id: runId, limit: 100 }).then((events) => clear(eventsEl, events.map(eventItem))).catch(() => {});

  const offOpen = ws.onOpen(subscribe);
  const offWs = listen({
    points: (m) => {
      if (m.run_id !== runId) return;
      for (const p of plots) if (p.has(m.series_id)) p.appendPoints(m.series_id, m.x, m.y);
    },
    "series.reload": (m) => {
      if (m.run_id !== runId) return;
      for (const p of plots) if (p.has(m.series_id)) p.load().then(subscribe);
    },
    "series.updated": (m) => { if (m.run_id === runId) refreshDetail(); },
    "run.progress": (m) => { if (m.run_id === runId) refreshDetail(); },
    "backfill.progress": (m) => {
      if (m.run_id !== runId) return;
      refreshDetail();
      // Backfill adds older points: reload the plots, at most every 10 s, and at the end.
      if (m.done) { clearTimeout(backfillTimer); backfillTimer = null; reloadAll(); }
      else if (!backfillTimer) backfillTimer = setTimeout(() => { backfillTimer = null; reloadAll(); }, BACKFILL_RELOAD_MS);
    },
    event: (e) => { if (e.run_id === runId) { eventsEl.prepend(eventItem(e)); refreshDetail(); } },
  });

  return () => {
    offOpen();
    offWs();
    clearTimeout(backfillTimer);
    ws.send({ type: "unsubscribe", run_id: runId });
    plots.forEach((p) => p.destroy());
  };
}
