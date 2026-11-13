// Entry point: hash router, connection status, authentication dialog.

import * as ws from "./ws.js";
import * as auth from "./auth.js";
import { clear, h } from "./util.js";
import { dashboardView } from "./views/dashboard.js";
import { runsView } from "./views/runs.js";
import { runView } from "./views/run.js";
import { compareView } from "./views/compare.js";
import { clustersView } from "./views/clusters.js";
import { newRunView } from "./views/newrun.js";
import { campaignsView, campaignView } from "./views/campaigns.js";

const routes = [
  [/^#?\/?$/, "dashboard", () => dashboardView],
  [/^#\/runs$/, "runs", () => runsView],
  [/^#\/runs\/new$/, "runs", () => newRunView],
  [/^#\/runs\/(\d+)$/, "runs", () => runView],
  [/^#\/compare$/, "runs", () => compareView],
  [/^#\/clusters$/, "clusters", () => clustersView],
  [/^#\/campaigns$/, "campaigns", () => campaignsView],
  [/^#\/campaigns\/(\d+)$/, "campaigns", () => campaignView],
];

let teardown = null;
let connected = false;

function updateStatus() {
  const pending = auth.pendingCount();
  clear(document.getElementById("status"),
    pending ? h("span", { class: "badge warn" }, `${pending} authentication pending`) : null, " ",
    h("span", { class: `badge ${connected ? "ok" : "bad"}` }, connected ? "live" : "offline"));
}

async function route() {
  if (teardown) { try { teardown(); } catch (err) { console.error(err); } teardown = null; }
  const [path, queryString] = location.hash.split("?");
  const main = document.getElementById("view");
  const match = routes.find(([re]) => re.test(path || ""));
  for (const a of document.querySelectorAll("[data-nav]")) {
    a.classList.toggle("active", !!match && a.dataset.nav === match[1]);
  }
  if (!match) {
    clear(main, h("p", {}, "Page not found. ", h("a", { href: "#/" }, "Dashboard")));
    return;
  }
  const params = (path || "").match(match[0]).slice(1);
  const view = match[2]();
  clear(main, h("p", { class: "muted" }, "Loading…"));
  try {
    teardown = (await view(main, { params, query: new URLSearchParams(queryString || "") })) || null;
  } catch (err) {
    clear(main, h("p", { class: "error" }, String(err.message || err)));
  }
}

ws.onStatus((ok) => { connected = ok; updateStatus(); });
auth.install(updateStatus);
ws.connect();
window.addEventListener("hashchange", route);
updateStatus();
route();
