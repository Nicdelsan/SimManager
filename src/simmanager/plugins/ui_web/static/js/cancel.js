// "Cancel job": the only action of the UI that changes the cluster. The server issues a
// single-use confirmation id; this dialog shows job id, name and cluster before using it.

import { api } from "./api.js";
import { h } from "./util.js";

function confirmDialog(title, lines, okText) {
  return new Promise((resolve) => {
    const finish = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    const dialog = h("dialog", { class: "confirm" },
      h("h2", {}, title),
      lines.filter(Boolean).map((line) => h("p", { class: line.cls || "" }, line.text || line)),
      h("div", { class: "actions" },
        h("button", { type: "button", onclick: () => finish(false) }, "Keep the job"),
        h("button", { type: "button", class: "danger", onclick: () => finish(true) }, okText)));
    dialog.addEventListener("cancel", () => finish(false));
    document.body.append(dialog);
    dialog.showModal();
  });
}

/** Returns the scancel result, or null when the user did not confirm. */
export async function cancelJob(clusterId, jobId) {
  let info;
  try {
    info = await api.prepareCancel({ cluster_id: clusterId, job_id: jobId });
  } catch (err) {
    alert(`Cannot cancel job ${jobId}: ${err.message}`);
    return null;
  }
  const ok = await confirmDialog(`Cancel job ${info.job_id}?`, [
    { text: `${info.name}  ·  ${info.state}`, cls: "mono" },
    { text: `Cluster ${info.cluster_id}`, cls: "mono" },
    info.work_dir ? { text: info.work_dir, cls: "mono small" } : null,
    { text: "SimManager runs `scancel` on the cluster and records it in the event log.", cls: "muted small" },
  ], `Cancel job ${info.job_id}`);
  if (!ok) return null;
  try {
    const result = await api.confirmAction(info.action_id);
    if (result.exit_status !== 0) alert(`scancel exited with ${result.exit_status}: ${result.stderr || result.stdout}`);
    return result;
  } catch (err) {
    alert(`Job ${jobId} not cancelled: ${err.message}`);
    return null;
  }
}
