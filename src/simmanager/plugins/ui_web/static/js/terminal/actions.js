// Quick actions of a terminal tab opened from a run: drop-down menus that *insert* a
// command into the terminal without Enter. The user decides whether to run it.

import { api } from "../api.js";
import { h } from "../util.js";

const cache = new Map(); // run id -> Promise of the quick-actions payload

function load(runId) {
  if (!cache.has(runId)) {
    const promise = api.quickActions(runId);
    // Retry on the next state change after an error, or while inputs could not be listed.
    promise.then((data) => { if (data.note) cache.delete(runId); }, () => cache.delete(runId));
    cache.set(runId, promise);
  }
  return cache.get(runId);
}

export function quickActions(tab) {
  const runId = tab.info.run_id;
  if (!runId) return null;
  const box = h("span", { class: "quick" }, h("span", { class: "muted" }, "…"));
  load(runId).then((data) => box.replaceChildren(...render(tab, data)))
    .catch((err) => box.replaceChildren(h("span", { class: "error" }, err.message)));
  return box;
}

function render(tab, data) {
  const groups = new Map();
  for (const action of data.actions) {
    if (!groups.has(action.group)) groups.set(action.group, []);
    groups.get(action.group).push(action);
  }
  const nodes = [h("span", { class: "muted" }, `run #${data.run_id}`)];
  for (const [group, actions] of groups) {
    const select = h("select", {
      title: "Writes the command into the terminal without running it: press Enter to run",
      onchange: () => {
        const action = actions[select.selectedIndex - 1];
        select.selectedIndex = 0;
        if (action) tab.insert(action.command);
      },
    }, h("option", { value: "" }, `${group} ▾`), actions.map((a) => h("option", {}, a.label)));
    nodes.push(select);
  }
  if (data.note) nodes.push(h("span", { class: "muted", title: data.note }, "(inputs: n/a)"));
  return nodes;
}
