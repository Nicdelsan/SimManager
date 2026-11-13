// Excel campaigns (M5): list, and per campaign the case table (computed state next to the
// manual state from the sheet), never-launched cases, runs not associated with any case,
// and manual association. Associations only change SimManager's database.

import { api } from "../api.js";
import { h, clear, debounce, basename, fmtTime, pct, progressBar, stateBadge } from "../util.js";
import { listen } from "./common.js";

const CASE_STATE_CLASS = {
  planned: "", queued: "info", running: "ok", completed: "ok",
  stopped: "warn", diverged: "bad", failed: "bad", cancelled: "bad", unknown: "",
};

function caseBadge(state) {
  return h("span", { class: `badge ${CASE_STATE_CLASS[state] || ""}` }, state);
}

const MAX_PARAM_COLUMNS = 4;

export async function campaignsView(main) {
  const list = await api.campaigns();
  clear(main, h("div", { class: "panel" },
    h("h2", {}, "Campaigns"),
    list.length
      ? h("table", {},
          h("thead", {}, h("tr", {}, ["campaign", "Excel file", "sheet", "work root", "updated"].map((t) => h("th", {}, t)))),
          h("tbody", {}, list.map((c) => h("tr", { class: "clickable", onclick: () => { location.hash = `#/campaigns/${c.id}`; } },
            h("td", {}, h("strong", {}, c.name)),
            h("td", { class: "mono small", title: c.source_path || "" }, basename(c.source_path || "—")),
            h("td", {}, c.sheet_name || "—"),
            h("td", { class: "mono small" }, c.work_root || "—"),
            h("td", { class: "small" }, fmtTime(c.updated_at))))))
      : h("p", { class: "muted" }, "No campaigns yet: `simman campaign import <file.xlsx>`.")));
  return null;
}

function runCell(r, onUnmatch) {
  const solver = r.solver;
  return h("div", { class: "small" },
    h("a", { href: `#/runs/${r.run_id}` }, `#${r.run_id}`), " ",
    r.job ? h("span", { class: "mono" }, `job ${r.job.job_id} `) : null,
    r.job ? stateBadge(r.job.state) : null, " ",
    solver ? stateBadge(solver.state) : null,
    solver && solver.progress !== null ? [" ", progressBar(solver.progress), " ", pct(solver.progress)] : null,
    r.nan_detected ? h("span", { class: "badge bad" }, "NaN") : null, " ",
    h("span", { class: "muted", title: r.match_detail || "" }, r.locked ? `🔒 ${r.match_kind}` : r.match_kind), " ",
    h("button", { class: "small", title: "Remove this association (the run will not be matched automatically again)", onclick: () => onUnmatch(r.run_id) }, "×"));
}

export async function campaignView(main, { params }) {
  const id = Number(params[0]);
  let data = null;
  const filter = { text: "", state: "" };
  const headerEl = h("div", { class: "panel" });
  const casesEl = h("div", { class: "panel" });
  const unlaunchedEl = h("div", { class: "panel" });
  const unmatchedEl = h("div", { class: "panel" });
  const errorEl = h("p", { class: "error" });
  clear(main, headerEl, errorEl, casesEl, unlaunchedEl, unmatchedEl);

  const act = async (fn) => {
    errorEl.textContent = "";
    try { await fn(); await load(); } catch (err) { errorEl.textContent = err.message; }
  };
  const unmatch = (runId) => {
    if (!confirm(`Remove run #${runId} from its case? It will not be matched automatically again for this campaign.`)) return;
    act(() => api.unmatchRun(id, runId));
  };
  const match = (caseKey, runId) => act(() => api.matchRun(id, { case_key: caseKey, run_id: runId }));

  const renderHeader = () => {
    const c = data.campaign;
    const counts = {};
    for (const k of data.cases) counts[k.computed_state] = (counts[k.computed_state] || 0) + 1;
    clear(headerEl,
      h("h1", {}, `Campaign ${c.name}`),
      h("div", { class: "row small" },
        h("span", { class: "mono", title: c.source_path || "" }, basename(c.source_path || "")),
        c.sheet_name ? h("span", {}, `sheet ${c.sheet_name}`) : null,
        c.work_root ? h("span", { class: "mono" }, `work root ${c.work_root}`) : null,
        h("span", {}, `${data.cases.length} cases`),
        Object.entries(counts).map(([s, n]) => h("span", {}, caseBadge(s), ` ${n}`))));
  };

  const renderCases = () => {
    const columns = (data.campaign.columns || [])
      .filter((col) => col !== data.campaign.case_column && col !== data.campaign.manual_state_column)
      .slice(0, MAX_PARAM_COLUMNS);
    const text = filter.text.toLowerCase();
    const rows = data.cases.filter((k) =>
      (!filter.state || k.computed_state === filter.state) &&
      (!text || k.case_key.toLowerCase().includes(text) ||
        Object.values(k.params || {}).some((v) => String(v).toLowerCase().includes(text))));
    const states = [...new Set(data.cases.map((k) => k.computed_state))].sort();
    clear(casesEl,
      h("div", { class: "toolbar" },
        h("h2", {}, "Cases"),
        h("input", { type: "text", placeholder: "filter (case or parameter)", value: filter.text,
          oninput: debounce((e) => { filter.text = e.target.value; renderCases(); }, 200) }),
        h("select", { onchange: (e) => { filter.state = e.target.value; renderCases(); } },
          h("option", { value: "" }, "all states"),
          states.map((s) => h("option", { value: s, selected: s === filter.state }, s))),
        h("span", { class: "muted small" }, `${rows.length} shown`)),
      h("table", {},
        h("thead", {}, h("tr", {},
          ["case", "parent", "computed state", "manual state", ...columns, "runs"].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows.map((k) => h("tr", {},
          h("td", { title: k.notes || "" }, h("strong", {}, k.case_key), k.active ? null : h("span", { class: "badge" }, "removed")),
          h("td", { class: "small" }, k.parent_key || ""),
          h("td", {}, caseBadge(k.computed_state)),
          h("td", { class: "small" }, k.manual_state || ""),
          columns.map((col) => h("td", { class: "small wrap" }, (k.params || {})[col] ?? "")),
          h("td", { class: "wrap" }, k.runs.length
            ? k.runs.map((r) => runCell(r, unmatch))
            : h("span", { class: "muted small" }, "never launched")))))));
    if (!rows.length) casesEl.append(h("p", { class: "muted" }, "No cases match the filter."));
  };

  const renderUnlaunched = () => {
    clear(unlaunchedEl,
      h("h2", {}, `Never launched (${data.unlaunched.length})`),
      data.unlaunched.length
        ? h("p", { class: "mono small wrap" }, data.unlaunched.join("  "))
        : h("p", { class: "muted" }, "Every case has at least one run."));
  };

  const renderUnmatched = () => {
    const caseKeys = data.cases.filter((k) => k.active).map((k) => k.case_key);
    const listId = `case-keys-${id}`;
    clear(unmatchedEl,
      h("h2", {}, `Runs not associated (${data.unmatched_runs.length})`),
      h("p", { class: "small muted" },
        "Runs SimManager knows that no case of this campaign claims. Associate one by typing a case; ",
        "the association is locked and not changed by automatic matching."),
      h("datalist", { id: listId }, caseKeys.map((k) => h("option", { value: k }))),
      data.unmatched_runs.length
        ? h("table", {},
            h("thead", {}, h("tr", {}, ["run", "cluster", "job", "directory", "ambiguous candidates", "associate with case"].map((t) => h("th", {}, t)))),
            h("tbody", {}, data.unmatched_runs.map((r) => {
              const input = h("input", { type: "text", list: listId, placeholder: "case", value: r.ambiguous_case_keys[0] || "" });
              return h("tr", {},
                h("td", {}, h("a", { href: `#/runs/${r.run_id}` }, `#${r.run_id}`)),
                h("td", {}, r.cluster_id || "—"),
                h("td", { class: "mono small" }, r.job ? `${r.job.job_id} ${r.job.name}` : r.origin),
                h("td", { class: "mono small wrap" }, r.work_dir || ""),
                h("td", { class: "small" }, r.ambiguous_case_keys.join(", ")),
                h("td", {}, input, " ", h("button", {
                  onclick: () => {
                    const key = input.value.trim();
                    if (!caseKeys.includes(key)) { errorEl.textContent = `Unknown case ${JSON.stringify(key)}.`; return; }
                    match(key, r.run_id);
                  },
                }, "Associate")));
            })))
        : h("p", { class: "muted" }, "None."));
  };

  const load = async () => {
    data = await api.campaign(id);
    renderHeader();
    renderCases();
    renderUnlaunched();
    renderUnmatched();
  };
  await load();

  const refresh = debounce(() => load().catch((err) => { errorEl.textContent = err.message; }), 1500);
  return listen({
    event: (e) => {
      if (e.kind.startsWith("case.") || e.kind.startsWith("campaign.") || e.kind.startsWith("run.") ||
          e.kind.startsWith("solver.") || e.kind.startsWith("job.")) refresh();
    },
  });
}
