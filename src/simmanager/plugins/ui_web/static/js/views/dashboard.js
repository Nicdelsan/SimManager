import { api } from "../api.js";
import { h, clear, debounce } from "../util.js";
import { clusterCard, eventItem, listen, runsTable } from "./common.js";

const LIVE_SOLVER = new Set(["running", "unknown", "starting"]);

function isActive(run) {
  if (run.job && run.job.active) return true;
  return !run.job && (!run.solver || LIVE_SOLVER.has(run.solver.state));
}

export async function dashboardView(main) {
  const clustersEl = h("div", { class: "clusters" });
  const runsEl = h("div", {});
  const eventsEl = h("ul", { class: "events" });
  clear(main,
    h("div", { class: "panel" }, h("h2", {}, "Clusters"), clustersEl),
    h("div", { class: "panel" },
      h("div", { class: "toolbar" }, h("h2", {}, "Active runs"), h("a", { href: "#/runs/new" }, "+ register a run")),
      runsEl),
    h("div", { class: "panel" }, h("h2", {}, "Latest events"), eventsEl));

  const loadClusters = async () => {
    try {
      const clusters = await api.clusters();
      clear(clustersEl, clusters.length
        ? clusters.map((c) => clusterCard(c, { onReconnect: loadClusters }))
        : h("p", { class: "muted" }, "No clusters configured: `simman cluster add`."));
    } catch (err) { clear(clustersEl, h("p", { class: "error" }, err.message)); }
  };
  const loadRuns = async () => {
    try {
      const runs = (await api.runs({ limit: 200 })).filter(isActive);
      clear(runsEl, runs.length ? runsTable(runs) : h("p", { class: "muted" }, "No active runs."));
    } catch (err) { clear(runsEl, h("p", { class: "error" }, err.message)); }
  };
  const loadEvents = async () => {
    const events = await api.events({ limit: 40 });
    clear(eventsEl, events.map(eventItem));
  };
  await Promise.all([loadClusters(), loadRuns(), loadEvents().catch(() => {})]);

  const refreshClusters = debounce(loadClusters, 300);
  const refreshRuns = debounce(loadRuns, 1000);
  return listen({
    "cluster.state": refreshClusters,
    "monitor.polled": () => { refreshClusters(); refreshRuns(); },
    "run.progress": refreshRuns,
    event: (e) => {
      eventsEl.prepend(eventItem(e));
      while (eventsEl.children.length > 100) eventsEl.lastChild.remove();
      if (e.kind.startsWith("job.") || e.kind.startsWith("run.") || e.kind.startsWith("solver.")) refreshRuns();
    },
  });
}
