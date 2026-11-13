import { api } from "../api.js";
import { h, clear, debounce } from "../util.js";
import { listen, runsTable } from "./common.js";

export async function runsView(main) {
  const selected = new Set();
  const compareBtn = h("button", {
    class: "primary",
    disabled: true,
    onclick: () => { location.hash = `#/compare?runs=${[...selected].join(",")}`; },
  }, "Compare selected");
  const tableEl = h("div", {});
  clear(main, h("div", { class: "panel" },
    h("div", { class: "toolbar" },
      h("h2", {}, "Runs"), compareBtn, h("a", { href: "#/runs/new" }, "+ register a run")),
    tableEl));

  const onSelect = (id, on) => {
    if (on) selected.add(id); else selected.delete(id);
    compareBtn.disabled = selected.size < 2;
  };
  const load = async () => {
    const runs = await api.runs({ limit: 500 });
    selected.clear();
    compareBtn.disabled = true;
    clear(tableEl, runs.length
      ? runsTable(runs, { selectable: true, selected: onSelect })
      : h("p", { class: "muted" }, "No runs yet."));
  };
  await load();
  const refresh = debounce(() => load().catch(() => {}), 2000);
  return listen({ event: (e) => { if (e.kind === "run.created") refresh(); } });
}
