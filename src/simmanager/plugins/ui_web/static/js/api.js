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
};
