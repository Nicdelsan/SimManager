// Script editor of the Display panel: library picker, the Python script, the shell setup lines
// (e.g. `module load python`), versions with diffs, "Run" and auto-refresh.
// Every save of a changed script is a new immutable version; executions record which one ran.

import { api } from "../api.js";
import { h, clear, fmtTime } from "../util.js";

const NEW = "";
// One string per line: a Python `import` at the start of a line would look like an ES import.
const DEFAULT_BODY = [
  "# Runs on the cluster login node, in the run folder.",
  "# Save png/gif/jpg/webp files anywhere below it (e.g. post/): new ones are shown here.",
  "import os",
  "import matplotlib",
  "matplotlib.use('Agg')",
  "import matplotlib.pyplot as plt",
  "",
  "os.makedirs('post', exist_ok=True)",
  "fig, ax = plt.subplots()",
  "ax.plot([0, 1, 2], [0, 1, 4])",
  "fig.savefig('post/example.png', dpi=120)",
  "",
].join("\n");

function storageKey(runId) { return `simman.run.${runId}.display.script`; }

function remember(runId, scriptId) {
  try { localStorage.setItem(storageKey(runId), String(scriptId)); } catch { /* private mode */ }
}

function remembered(runId) {
  try { return Number(localStorage.getItem(storageKey(runId))) || null; } catch { return null; }
}

export function scriptEditor(runId, { onStarted }) {
  const picker = h("select", { onchange: () => pick(picker.value ? Number(picker.value) : null) });
  const name = h("input", { type: "text", placeholder: "script name, e.g. slice-T" });
  const interpreter = h("input", { type: "text", value: "python3", title: "Program that runs the script, e.g. python3 or /path/to/python -u" });
  const setup = h("textarea", { rows: 2, class: "mono", placeholder: "module load python/3.11   (shell lines run first, optional)" });
  const body = h("textarea", { rows: 16, class: "mono", spellcheck: "false" });
  const message = h("input", { type: "text", placeholder: "what changed (optional)" });
  const status = h("span", { class: "small muted" });
  const runButton = h("button", { type: "button", class: "primary", onclick: () => run() }, "Run on cluster");
  const saveButton = h("button", { type: "button", onclick: () => save() }, "Save version");
  const autoBox = h("input", { type: "checkbox", onchange: () => toggleAuto() });
  const autoMinutes = h("input", { type: "number", min: 1, value: 10, class: "display-minutes" });
  const autoInfo = h("span", { class: "small muted" });
  const versionsEl = h("div", { class: "small" });
  const diffEl = h("pre", { class: "display-out", hidden: true });

  let scripts = [];
  let current = null; // the saved version shown in the editor (null = unsaved new script)
  let schedules = [];

  const el = h("div", { class: "display-editor" },
    h("div", { class: "toolbar" }, h("strong", {}, "Script"), picker),
    h("label", { class: "field" }, h("span", {}, "Name"), name),
    h("label", { class: "field" }, h("span", {}, "Setup (shell)"), setup),
    h("label", { class: "field" }, h("span", {}, "Python script"), body),
    h("div", { class: "row" },
      h("label", { class: "field grow" }, h("span", {}, "Interpreter"), interpreter),
      h("label", { class: "field grow" }, h("span", {}, "Version note"), message)),
    h("div", { class: "toolbar" }, runButton, saveButton, status),
    h("div", { class: "toolbar small" },
      h("label", {}, autoBox, " Auto-refresh every "), autoMinutes, h("span", {}, " min while the job runs"), autoInfo),
    h("details", {}, h("summary", { class: "small" }, "Versions"), versionsEl, diffEl));

  const fields = () => ({
    name: name.value.trim(), body: body.value, setup: setup.value,
    interpreter: interpreter.value.trim() || "python3", message: message.value.trim(),
  });

  const norm = (text) => {
    const t = (text || "").replace(/\r\n?/g, "\n");
    return t && !t.endsWith("\n") ? `${t}\n` : t;
  };

  const dirty = () => {
    if (!current) return true;
    const f = fields();
    return norm(f.body) !== current.body || norm(f.setup) !== current.setup
      || f.interpreter !== current.interpreter || f.name !== currentName();
  };

  const currentName = () => (scripts.find((s) => s.id === (current && current.script_id)) || {}).name || "";

  const fill = (version, scriptName) => {
    name.value = scriptName;
    body.value = version.body;
    setup.value = version.setup;
    interpreter.value = version.interpreter;
    message.value = "";
  };

  const renderPicker = () => {
    const selected = current ? current.script_id : null;
    clear(picker,
      h("option", { value: NEW, selected: selected === null }, "— new script —"),
      scripts.map((s) => h("option", { value: String(s.id), selected: s.id === selected }, `${s.name} (v${s.latest.number})`)));
  };

  const renderVersions = async () => {
    diffEl.hidden = true;
    if (!current) { clear(versionsEl, h("span", { class: "muted" }, "Not saved yet.")); return; }
    const list = await api.displayVersions(current.script_id);
    clear(versionsEl, h("ul", { class: "display-versions" }, list.map((v, i) => h("li", {},
      h("strong", {}, `v${v.number}`), ` ${fmtTime(v.created_at)} `,
      h("span", { class: "mono muted" }, v.sha256.slice(0, 8)), " ",
      v.message ? h("span", {}, v.message) : null, " ",
      h("button", { type: "button", title: "Put this version in the editor (Save makes it the newest)",
        onclick: () => { fill(v, currentName()); status.textContent = `v${v.number} loaded: Save to make it current`; } }, "Load"),
      list[i + 1]
        ? h("button", { type: "button", onclick: () => showDiff(list[i + 1].id, v.id) }, `Diff v${list[i + 1].number}→v${v.number}`)
        : null))));
  };

  const showDiff = async (oldId, newId) => {
    try {
      const d = await api.displayDiff(oldId, newId);
      diffEl.textContent = d.diff || "(no differences)";
      diffEl.hidden = false;
    } catch (err) { alert(err.message); }
  };

  const renderAuto = () => {
    const s = current && schedules.find((x) => x.script_id === current.script_id);
    autoBox.checked = Boolean(s && s.enabled);
    if (s) autoMinutes.value = String(Math.round(s.interval_s / 60));
    autoInfo.textContent = s && s.enabled && s.last_run_at ? ` · last ${fmtTime(s.last_run_at)}` : "";
  };

  async function pick(scriptId) {
    const script = scripts.find((s) => s.id === scriptId);
    current = script ? script.latest : null;
    if (script) { fill(script.latest, script.name); remember(runId, script.id); }
    else { fill({ body: DEFAULT_BODY, setup: "", interpreter: "python3" }, ""); }
    status.textContent = "";
    renderPicker();
    renderAuto();
    await renderVersions().catch(() => {});
  }

  async function load() {
    try { scripts = await api.displayScripts(); } catch (err) { status.textContent = err.message; return; }
    const wanted = remembered(runId);
    await pick(scripts.some((s) => s.id === wanted) ? wanted : scripts[0] ? scripts[0].id : null);
  }

  /** Saves when changed; returns the version to run (or null on error). */
  async function save() {
    const f = fields();
    if (!f.name) { alert("Give the script a name: it is kept in the library."); name.focus(); return null; }
    if (!dirty()) { status.textContent = `no changes (v${current.number})`; return current; }
    try {
      const result = await api.saveDisplayScript(f);
      current = result.version;
      scripts = await api.displayScripts();
      remember(runId, current.script_id);
      renderPicker();
      renderAuto();
      await renderVersions();
      message.value = "";
      status.textContent = result.created ? `saved as v${current.number}` : `unchanged (v${current.number})`;
      return current;
    } catch (err) {
      alert(`Not saved: ${err.message}`);
      return null;
    }
  }

  async function run() {
    runButton.disabled = true;
    try {
      const version = await save();
      if (!version) return;
      await api.runDisplayScript(runId, version.script_id);
      status.textContent = `v${version.number} running on the cluster…`;
      if (onStarted) onStarted();
    } catch (err) {
      alert(`Not started: ${err.message}`);
    } finally {
      runButton.disabled = false;
    }
  }

  async function toggleAuto() {
    const enable = autoBox.checked;
    const minutes = Math.max(1, Math.round(Number(autoMinutes.value) || 10));
    const version = enable ? await save() : current;
    if (!version) { autoBox.checked = false; return; }
    if (enable && !confirm(`Run "${currentName()}" on the cluster every ${minutes} min while this job is running?\n`
      + "It always runs the newest saved version. Each execution is recorded in the event log.")) {
      autoBox.checked = false;
      return;
    }
    try {
      await api.scheduleDisplay(runId, { script_id: version.script_id, interval_s: minutes * 60, enabled: enable });
      if (onStarted) onStarted();
    } catch (err) {
      alert(err.message);
      autoBox.checked = !enable;
    }
  }

  function setSchedules(list, busy) {
    schedules = list || [];
    runButton.disabled = Boolean(busy);
    if (!busy && status.textContent.endsWith("running on the cluster…")) status.textContent = "";
    renderAuto();
  }

  return { el, load, setSchedules };
}
