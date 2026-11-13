// One terminal session: an xterm.js screen bound to /ws/term/{id}.
//
// SimManager never types into the terminal: bytes are sent only for the user's keys,
// pastes and quick-action clicks (which insert text without Enter).

import { Terminal } from "../../vendor/xterm-6.0.0/xterm.mjs";
import { FitAddon } from "../../vendor/xterm-6.0.0/addon-fit.mjs";
import { api } from "../api.js";
import { h, clear, debounce } from "../util.js";

const encoder = new TextEncoder();
const MAX_FRAME = 60 * 1024; // the server accepts up to 64 KiB per input frame

const THEME = {
  background: "#0b0f17", foreground: "#e5e7eb", cursor: "#e5e7eb",
  selectionBackground: "#3b4b6b",
};

const STATE_DOT = { open: "ok", connecting: "warn", awaiting_auth: "warn", exited: "", failed: "bad", closed: "" };

export class TerminalTab {
  /**
   * @param info session JSON from the API
   * @param hooks {onChange(tab), onClosed(tab), onReopen(request), toolbar(tab) -> Node[]}
   */
  constructor(info, hooks) {
    this.info = info;
    this.hooks = hooks;
    this.ws = null;
    this.closedByUser = false;
    this.retryTimer = null;

    this.term = new Terminal({
      cursorBlink: true,
      fontFamily: "Cascadia Mono, Consolas, ui-monospace, monospace",
      fontSize: 13,
      scrollback: 10000,
      theme: THEME,
    });
    this.fit = new FitAddon();
    this.term.loadAddon(this.fit);

    this.dot = h("span", { class: "dot" });
    this.label = h("span", {}, info.title);
    this.tabEl = h("div", { class: "tab", title: info.cwd || "home" },
      this.dot, this.label,
      h("button", { type: "button", class: "close", title: "Close this terminal",
        onclick: (e) => { e.stopPropagation(); this.close(); } }, "×"));

    this.whereEl = h("span", { class: "where" });
    this.extraEl = h("span", { class: "extra" });
    this.messageEl = h("div", { class: "message", hidden: true });
    this.screenEl = h("div", { class: "screen" });
    this.paneEl = h("section", { class: "pane", hidden: true },
      h("div", { class: "tbar" }, this.whereEl, this.extraEl, h("span", { class: "spacer" })),
      this.messageEl,
      this.screenEl);
    this.toolbarEl = this.paneEl.firstChild;

    this.term.open(this.screenEl);
    this.term.onData((data) => this.send(encoder.encode(data)));
    this.term.onBinary((data) => this.send(Uint8Array.from(data, (c) => c.charCodeAt(0) & 255)));
    this.term.onResize(({ cols, rows }) => this.sendControl({ type: "resize", cols, rows }));
    this.term.attachCustomKeyEventHandler((event) => this.onKey(event));
    this.refit = debounce(() => this.fitNow(), 80);
    this.update(info);
  }

  // ------------------------------------------------------------------ attach
  async attach(ticket) {
    clearTimeout(this.retryTimer);
    if (!ticket) {
      try { ticket = (await api.terminalTicket(this.info.id)).ticket; }
      catch (err) {
        if (err.status === 404) { this.update({ ...this.info, state: "closed", detail: "session ended" }); return; }
        this.showMessage(`Cannot reach SimManager: ${err.message}`, "bad", "Retry", () => this.attach());
        return;
      }
    }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/ws/term/${encodeURIComponent(this.info.id)}?ticket=${encodeURIComponent(ticket)}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.addEventListener("message", (event) => {
      if (typeof event.data === "string") this.onControl(JSON.parse(event.data));
      else this.term.write(new Uint8Array(event.data)); // xterm decodes UTF-8 across frames
    });
    ws.addEventListener("open", () => this.fitNow());
    ws.addEventListener("close", (event) => this.onSocketClosed(ws, event));
  }

  onControl(message) {
    if (message.type === "replay") {
      this.term.reset();
      // Full-screen programs (vi, top) redraw on SIGWINCH: nudge the size once the
      // recent output has been replayed.
      setTimeout(() => this.nudge(), 150);
    } else if (message.type === "state") {
      this.update(message);
    }
  }

  onSocketClosed(ws, event) {
    if (this.ws !== ws) return;
    this.ws = null;
    if (this.closedByUser || this.info.state === "closed") return;
    if (event.code === 4001) {
      this.showMessage("This terminal is now shown in another window.", "", "Show it here", () => this.attach());
    } else if (event.code === 1000) {
      this.update({ ...this.info, state: "closed" });
    } else {
      // SimManager restarted or the network blipped: try again with a fresh ticket.
      this.showMessage("Connection to SimManager lost, reconnecting…", "bad");
      this.retryTimer = setTimeout(() => this.attach(), 2000);
    }
  }

  // ------------------------------------------------------------------- input
  send(bytes) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    for (let i = 0; i < bytes.length; i += MAX_FRAME) this.ws.send(bytes.subarray(i, i + MAX_FRAME));
  }

  sendControl(message) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message));
  }

  /** Insert text as a paste, without Enter: the user decides whether to run it. */
  insert(text) {
    this.term.paste(text);
    this.term.focus();
  }

  onKey(event) {
    if (event.type !== "keydown") return true;
    const key = event.key.toLowerCase();
    if (event.ctrlKey && !event.altKey && key === "c" && (event.shiftKey || this.term.hasSelection())) {
      // Ctrl+C copies when something is selected (Windows habit); otherwise it is SIGINT.
      const text = this.term.getSelection();
      if (text) navigator.clipboard.writeText(text).catch(() => {});
      this.term.clearSelection();
      event.preventDefault();
      return false;
    }
    if (event.ctrlKey && !event.altKey && key === "v") {
      return false; // let the browser paste into xterm (bracketed paste when enabled)
    }
    return true;
  }

  // ----------------------------------------------------------------- display
  update(info) {
    this.info = { ...this.info, ...info };
    const s = this.info;
    this.label.textContent = s.title + (s.tmux_session ? ` (${s.tmux_session})` : "");
    this.dot.className = `dot ${STATE_DOT[s.state] || ""}`;
    this.whereEl.textContent = `${s.cluster_id}:${s.cwd || "~"}`;
    clear(this.extraEl, this.hooks.toolbar ? this.hooks.toolbar(this) : null);
    if (s.state === "connecting") this.showMessage(s.detail || "Connecting…");
    else if (s.state === "awaiting_auth") this.showMessage(`${s.detail}. Answer the authentication dialog.`);
    else if (s.state === "open") this.hideMessage();
    else if (s.state === "exited") this.showMessage(`${s.detail}.`, "", "Close", () => this.close());
    else if (s.state === "closed") this.showMessage(`Terminal closed${s.detail ? ` (${s.detail})` : ""}.`, "bad", "Remove", () => this.close());
    else if (s.state === "failed") this.showFailure(s);
    this.hooks.onChange(this);
  }

  showFailure(s) {
    const reopen = (extra) => { this.close(); this.hooks.onReopen({ cluster_id: s.cluster_id, run_id: s.run_id, ...extra }); };
    if (s.error === "missing_dir") {
      this.showMessage(`The folder ${s.cwd} does not exist any more.`, "bad", "Open in the home folder", () => reopen({}));
    } else if (s.error === "limit") {
      this.showMessage(`${s.detail}.`, "bad", "Close", () => this.close());
    } else {
      this.showMessage(`Cannot open the terminal: ${s.detail}`, "bad", "Retry", () => reopen({ cwd: s.cwd }));
    }
  }

  showMessage(text, kind = "", action = null, onAction = null) {
    clear(this.messageEl, text, action ? h("button", { type: "button", onclick: onAction }, action) : null);
    this.messageEl.className = `message ${kind}`;
    this.messageEl.hidden = false;
    this.refit();
  }

  hideMessage() {
    if (!this.messageEl.hidden) { this.messageEl.hidden = true; this.refit(); }
  }

  show(visible) {
    this.paneEl.hidden = !visible;
    this.tabEl.classList.toggle("active", visible);
    if (visible) { this.fitNow(); this.term.focus(); }
  }

  fitNow() {
    if (this.paneEl.hidden) return;
    try { this.fit.fit(); } catch { /* not laid out yet */ }
  }

  nudge() {
    const { cols, rows } = this.term;
    if (rows > 2) {
      this.sendControl({ type: "resize", cols, rows: rows - 1 });
      this.sendControl({ type: "resize", cols, rows });
    }
  }

  // ------------------------------------------------------------------- close
  async close() {
    if (this.info.state !== "closed" && !this.closedByUser) {
      const live = this.info.state === "open";
      if (live && !confirm(`Close the terminal ${this.info.title}? The shell on ${this.info.cluster_id} ends.`)) return;
      this.closedByUser = true;
      try { await api.closeTerminal(this.info.id); } catch { /* already gone */ }
    }
    clearTimeout(this.retryTimer);
    if (this.ws) this.ws.close();
    this.term.dispose();
    this.hooks.onClosed(this);
  }
}
