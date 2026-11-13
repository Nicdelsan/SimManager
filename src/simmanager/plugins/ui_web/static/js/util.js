// DOM and formatting helpers. Never build HTML from strings: all text goes through
// textContent, so data from clusters (job names, paths, log lines) cannot inject markup.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key === "list") el.setAttribute("list", value); // read-only property on inputs
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

export function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400);
  const hh = String(Math.floor((s % 86400) / 3600)).padStart(2, "0");
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  return d > 0 ? `${d}-${hh}:${mm}` : `${hh}:${mm}:${String(s % 60).padStart(2, "0")}`;
}

export function fmtRough(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return "—";
  if (seconds < 90) return `~${Math.round(seconds)} s`;
  if (seconds < 5400) return `~${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `~${(seconds / 3600).toFixed(1)} h`;
  return `~${(seconds / 86400).toFixed(1)} d`;
}

export function fmtTime(value) {
  if (value === null || value === undefined) return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (isNaN(date)) return "—";
  const today = new Date().toDateString() === date.toDateString();
  return today
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function fmtCount(n) {
  if (n === null || n === undefined) return "—";
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e4) return `${Math.round(n / 1e3)}k`;
  return String(n);
}

export function pct(fraction) {
  return fraction === null || fraction === undefined ? "—" : `${Math.round(fraction * 100)} %`;
}

export function progressBar(fraction) {
  const inner = h("span");
  inner.style.width = `${Math.max(0, Math.min(1, fraction || 0)) * 100}%`; // CSSOM: allowed by the CSP
  return h("span", { class: fraction >= 1 ? "bar done" : "bar" }, inner);
}

const STATE_CLASS = {
  connected: "ok", running: "ok", completed: "ok",
  connecting: "info", pending: "info", queued: "info", disconnected: "",
  backoff: "warn", awaiting_auth: "warn", stopped: "warn", suspended: "warn",
  failed: "bad", cancelled: "bad", timeout: "bad", node_fail: "bad", out_of_memory: "bad",
};

export function stateBadge(state, text) {
  return h("span", { class: `badge ${STATE_CLASS[state] || ""}` }, text || (state || "—").replaceAll("_", " "));
}

export function basename(path) {
  return (path || "").split("/").filter(Boolean).pop() || path;
}
