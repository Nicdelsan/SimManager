// Compare runs: series present in every selected run (same file name, series name and
// qualifier) overlaid in one plot per quantity.

import { api } from "../api.js";
import * as ws from "../ws.js";
import { Plot } from "../plot.js";
import { h, clear, basename } from "../util.js";
import { listen } from "./common.js";
import { seriesLabel } from "./run.js";

function key(s) {
  return `${basename(s.file_path)}|${s.name}|${s.qualifier}|${s.unit}|${s.x_name}`;
}

export async function compareView(main, { query }) {
  const ids = (query.get("runs") || "").split(",").map(Number).filter((n) => Number.isInteger(n) && n > 0);
  const allRuns = await api.runs({ limit: 500 });
  const picker = h("div", { class: "toolbar" },
    h("span", {}, "Runs:"),
    allRuns.map((r) => h("label", { class: "small" },
      h("input", {
        type: "checkbox",
        checked: ids.includes(r.id),
        onchange: (e) => {
          const next = e.target.checked ? [...ids, r.id] : ids.filter((i) => i !== r.id);
          location.hash = `#/compare?runs=${next.join(",")}`;
        },
      }), ` #${r.id} ${r.job ? r.job.name : basename(r.work_dir || "")}`, " ")));
  const choiceEl = h("div", { class: "toolbar" });
  const plotsEl = h("div", { class: "panel" });
  clear(main, h("div", { class: "panel" }, h("h2", {}, "Compare runs"), picker, choiceEl), plotsEl);

  if (ids.length < 2) {
    clear(plotsEl, h("p", { class: "muted" }, "Select at least two runs."));
    return null;
  }
  const seriesByRun = new Map(await Promise.all(ids.map(async (id) => [id, await api.runSeries(id)])));
  const common = new Map();
  for (const s of seriesByRun.get(ids[0])) {
    if (s.progress) continue;
    const k = key(s);
    const matches = ids.map((id) => seriesByRun.get(id).find((t) => key(t) === k));
    if (matches.every(Boolean)) common.set(k, matches);
  }
  if (!common.size) {
    clear(plotsEl, h("p", { class: "muted" }, "These runs have no series in common."));
    return null;
  }

  let plot = null;
  const subscribe = () => {
    if (!plot) return;
    for (const id of ids) {
      const series = plot.series.filter((s) => s.meta.run_id === id).map((s) => ({ id: s.meta.id, last_x: s.tailX }));
      ws.send({ type: "subscribe", run_id: id, series });
    }
  };
  const show = async (k) => {
    if (plot) plot.destroy();
    clear(plotsEl);
    const matches = common.get(k);
    plot = new Plot(plotsEl, {
      title: `${basename(matches[0].file_path)}: ${seriesLabel(matches[0])}${matches[0].unit ? ` (${matches[0].unit})` : ""}`,
      xLabel: matches[0].x_name,
      series: matches.map((s) => ({ meta: s, label: `run #${s.run_id}` })),
    });
    await plot.load();
    subscribe();
  };
  const select = h("select", { onchange: (e) => show(e.target.value) },
    [...common.entries()].map(([k, m]) => h("option", { value: k }, `${basename(m[0].file_path)}: ${seriesLabel(m[0])}`)));
  clear(choiceEl, h("span", {}, "Quantity:"), select);
  await show(select.value);

  const offOpen = ws.onOpen(subscribe);
  const off = listen({
    points: (m) => { if (plot && plot.has(m.series_id)) plot.appendPoints(m.series_id, m.x, m.y); },
    "series.reload": (m) => { if (plot && plot.has(m.series_id)) plot.load().then(subscribe); },
  });
  return () => {
    offOpen();
    off();
    for (const id of ids) ws.send({ type: "unsubscribe", run_id: id });
    if (plot) plot.destroy();
  };
}
