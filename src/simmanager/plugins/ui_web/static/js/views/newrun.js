import { api } from "../api.js";
import { h, clear } from "../util.js";

export async function newRunView(main) {
  const clusters = await api.clusters();
  const errorEl = h("p", { class: "error" });
  const cluster = h("select", { name: "cluster" }, clusters.map((c) => h("option", { value: c.id }, c.id)));
  const workDir = h("input", { type: "text", name: "work_dir", placeholder: "/absolute/path/of/the/case", required: true });
  const solver = h("select", { name: "solver" },
    h("option", { value: "" }, "detect automatically"),
    h("option", { value: "converge" }, "CONVERGE"),
    h("option", { value: "openfoam" }, "OpenFOAM"));
  const submit = h("button", { type: "submit", class: "primary" }, "Register and follow");
  const form = h("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      submit.disabled = true;
      errorEl.textContent = "";
      try {
        const run = await api.createRun({
          cluster_id: cluster.value,
          work_dir: workDir.value.trim(),
          solver: solver.value || null,
        });
        location.hash = `#/runs/${run.id}`;
      } catch (err) {
        errorEl.textContent = err.message;
      } finally {
        submit.disabled = false;
      }
    },
  },
  h("label", { class: "field" }, h("span", {}, "Cluster"), cluster),
  h("label", { class: "field" }, h("span", {}, "Case directory on the cluster"), workDir),
  h("label", { class: "field" }, h("span", {}, "Solver"), solver),
  submit, errorEl);

  clear(main, h("div", { class: "panel" },
    h("h2", {}, "Register a run"),
    h("p", { class: "small muted" },
      "For a simulation not started through SLURM (or not visible in the queue). ",
      "SimManager only reads the directory; it never writes on the cluster. The run is followed ",
      "until the solver finishes."),
    clusters.length ? form : h("p", { class: "muted" }, "No clusters configured: `simman cluster add`.")));
}
