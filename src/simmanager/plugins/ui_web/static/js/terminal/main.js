// Terminal page: tabs (one per session), "+ Terminal" menu, re-attach after reload.
//
// Other pages open terminals with `openTerminal()` from `./open.js`, which loads
// `/terminal#new?cluster=…&cwd=…&run=…` in the window named "simman-terminal".

import { api } from "../api.js";
import * as ws from "../ws.js";
import * as auth from "../auth.js";
import { h, clear, debounce } from "../util.js";
import { TerminalTab } from "./tab.js";
import { quickActions } from "./actions.js";

const tabs = new Map();
let active = null;
let connected = false;

const tabsEl = document.getElementById("tabs");
const panesEl = document.getElementById("panes");
const menuEl = document.getElementById("new-menu");
const emptyEl = panesEl.querySelector(".empty");

const hooks = {
  onChange: () => {},
  onClosed: (tab) => removeTab(tab),
  onReopen: (request) => openNew(request),
  toolbar: (tab) => quickActions(tab),
};

function updateStatus() {
  const pending = auth.pendingCount();
  clear(document.getElementById("status"),
    pending ? h("span", { class: "badge warn" }, `${pending} authentication pending`) : null, " ",
    h("span", { class: `badge ${connected ? "ok" : "bad"}` }, connected ? "live" : "offline"));
}

function addTab(info) {
  const tab = new TerminalTab(info, hooks);
  tab.tabEl.addEventListener("click", () => activate(tab));
  tabs.set(info.id, tab);
  tabsEl.append(tab.tabEl);
  panesEl.append(tab.paneEl);
  emptyEl.hidden = true;
  return tab;
}

function activate(tab) {
  if (active && active !== tab) active.show(false);
  active = tab;
  if (tab) tab.show(true);
  document.title = tab ? `${tab.info.title} · SimManager` : "SimManager terminal";
}

function removeTab(tab) {
  tabs.delete(tab.info.id);
  tab.tabEl.remove();
  tab.paneEl.remove();
  if (active === tab) activate([...tabs.values()].pop() || null);
  emptyEl.hidden = tabs.size > 0;
}

async function openNew(request) {
  try {
    const { session, ticket } = await api.openTerminal({ cols: 120, rows: 32, ...request });
    const tab = addTab(session);
    activate(tab);
    await tab.attach(ticket);
  } catch (err) {
    alert(`Cannot open a terminal: ${err.message}`);
  }
}

// ------------------------------------------------------------- "+ Terminal" menu
async function showMenu() {
  if (!menuEl.hidden) { menuEl.hidden = true; return; }
  clear(menuEl, h("div", { class: "group" }, "Loading…"));
  menuEl.hidden = false;
  let clusters;
  try { clusters = await api.clusters(); }
  catch (err) { clear(menuEl, h("div", { class: "group error" }, err.message)); return; }
  clear(menuEl, clusters.length ? clusters.map(clusterMenu) : h("div", { class: "group" }, "No clusters configured."));
}

function clusterMenu(c) {
  const t = c.terminals || {};
  const tmux = h("input", { type: "checkbox", checked: !!t.tmux });
  const pick = (cwd) => { menuEl.hidden = true; openNew({ cluster_id: c.id, cwd, tmux: tmux.checked }); };
  const tmuxList = h("div");
  const full = t.open >= t.max;
  return h("div", {},
    h("div", { class: "group" }, `${c.id} · ${c.state.replaceAll("_", " ")}`,
      ` · ${t.open || 0}/${t.max ?? "?"} terminals`),
    full ? h("div", { class: "group error" }, "Terminal limit reached: close one first.") : [
      h("button", { type: "button", class: "item", onclick: () => pick(null) }, "Home folder"),
      (c.work_roots || []).map((root) =>
        h("button", { type: "button", class: "item mono", onclick: () => pick(root) }, root)),
      h("label", { class: "option" }, tmux, " in a new tmux session"),
      h("button", { type: "button", class: "item", onclick: () => listTmux(c, tmuxList) },
        "Re-attach an existing tmux session…"),
      tmuxList,
    ]);
}

async function listTmux(c, target) {
  clear(target, h("div", { class: "group" }, "Asking the cluster…"));
  try {
    const names = await api.tmuxSessions(c.id);
    clear(target, names.length
      ? names.map((name) => h("button", { type: "button", class: "item mono",
          onclick: () => { menuEl.hidden = true; openNew({ cluster_id: c.id, attach_tmux: name }); } }, name))
      : h("div", { class: "group" }, "No simman tmux session on this cluster."));
  } catch (err) {
    clear(target, h("div", { class: "group error" }, err.message));
  }
}

// ------------------------------------------------------------------ routing
function handleHash() {
  const [path, query] = location.hash.split("?");
  if (path !== "#new") return;
  history.replaceState(null, "", "/terminal"); // a reload must not open it again
  const q = new URLSearchParams(query || "");
  const request = { cluster_id: q.get("cluster") };
  if (q.get("cwd")) request.cwd = q.get("cwd");
  if (q.get("run")) request.run_id = Number(q.get("run"));
  if (request.cluster_id) openNew(request);
}

async function restore() {
  // Sessions survive a reload of this page: show them all again (replay of recent output).
  for (const info of await api.terminals()) {
    if (info.state === "closed") continue;
    const tab = addTab(info);
    if (!active) activate(tab);
    await tab.attach();
  }
}

document.getElementById("new-button").addEventListener("click", showMenu);
document.addEventListener("click", (event) => {
  if (!menuEl.hidden && !event.target.closest(".new-terminal")) menuEl.hidden = true;
});
window.addEventListener("hashchange", handleHash);
window.addEventListener("resize", debounce(() => { if (active) active.fitNow(); }, 100));
ws.on("terminal.state", (m) => { const tab = tabs.get(m.id); if (tab && !tab.ws) tab.update(m); });
ws.onStatus((ok) => { connected = ok; updateStatus(); });
auth.install(updateStatus);
ws.connect();
updateStatus();
restore().catch((err) => alert(err.message)).finally(handleHash);
