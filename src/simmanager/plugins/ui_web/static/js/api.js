// REST client. Same-origin requests carry the session cookie automatically; POSTs are
// JSON (the server rejects anything else) and the browser adds the Origin header.

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
  }
}

async function request(method, path, body) {
  const init = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`/api${path}`, init);
  if (response.status === 401) {
    throw new ApiError(401, "Session expired: open the link printed by `simman serve`.");
  }
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!response.ok) {
    const detail = data && data.detail;
    throw new ApiError(response.status, typeof detail === "string" ? detail : JSON.stringify(detail || text));
  }
  return data;
}

function query(params) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const api = {
  clusters: () => request("GET", "/clusters"),
  reconnect: (id) => request("POST", `/clusters/${encodeURIComponent(id)}/reconnect`, {}),
  jobs: (params) => request("GET", `/jobs${query(params)}`),
  runs: (params) => request("GET", `/runs${query(params)}`),
  run: (id) => request("GET", `/runs/${id}`),
  runSeries: (id) => request("GET", `/runs/${id}/series`),
  createRun: (body) => request("POST", "/runs", body),
  points: (seriesId, params) => request("GET", `/series/${seriesId}/points${query(params)}`),
  events: (params) => request("GET", `/events${query(params)}`),
  campaigns: () => request("GET", "/campaigns"),
  campaign: (id) => request("GET", `/campaigns/${id}`),
  matchRun: (id, body) => request("POST", `/campaigns/${id}/match`, body),
  unmatchRun: (id, runId) => request("POST", `/campaigns/${id}/unmatch`, { run_id: runId }),
  terminals: () => request("GET", "/terminals"),
  openTerminal: (body) => request("POST", "/terminals", body),
  terminalTicket: (id) => request("POST", `/terminals/${encodeURIComponent(id)}/ticket`, {}),
  closeTerminal: (id) => request("POST", `/terminals/${encodeURIComponent(id)}/close`, {}),
  prepareCancel: (body) => request("POST", "/actions/job-cancel/prepare", body),
  confirmAction: (actionId) => request("POST", `/actions/${encodeURIComponent(actionId)}/confirm`, {}),
  quickActions: (runId) => request("GET", `/runs/${runId}/quick-actions`),
  tmuxSessions: (cluster) => request("GET", `/clusters/${encodeURIComponent(cluster)}/tmux`),
  displayScripts: () => request("GET", "/display/scripts"),
  saveDisplayScript: (body) => request("POST", "/display/scripts", body),
  displayVersions: (scriptId) => request("GET", `/display/scripts/${scriptId}/versions`),
  displayDiff: (oldId, newId) => request("GET", `/display/diff${query({ old: oldId, new: newId })}`),
  runDisplay: (runId) => request("GET", `/runs/${runId}/display`),
  runDisplayScript: (runId, scriptId) => request("POST", `/runs/${runId}/display/run`, { script_id: scriptId }),
  scheduleDisplay: (runId, body) => request("POST", `/runs/${runId}/display/schedule`, body),
  displayFiles: (runId, path) => request("GET", `/runs/${runId}/display/files${query({ path })}`),
  fetchDisplayImage: (imageId) => request("POST", `/display/images/${imageId}/fetch`, {}),
};

/** URLs used directly as <img src> (same origin: the session cookie goes along). */
export const displayUrls = {
  image: (imageId) => `/api/display/images/${imageId}`,
  file: (runId, path, force) => `/api/runs/${runId}/display/file${query({ path, force: force ? "true" : "" })}`,
};

/** Status and detail of a failed GET (e.g. 413 with {size, limit} for a too large image). */
export async function probe(url) {
  const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (response.ok) return { ok: true, status: response.status };
  let detail = null;
  try { detail = (await response.json()).detail; } catch { detail = null; }
  return { ok: false, status: response.status, detail };
}
