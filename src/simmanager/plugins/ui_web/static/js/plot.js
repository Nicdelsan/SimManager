// One uPlot chart for a group of series sharing unit and x axis.
//
// Data comes from /api/series/{id}/points with the visible window and the pixel width:
// the server picks the stored resolution level (raw, 1:16, 1:256 min/max). Zooming
// (drag on the chart) reloads the window at higher resolution; "Reset" goes back to the
// full range. Live points are appended by the caller (appendPoints).

import uPlot from "../vendor/uplot-1.6.31/uPlot.esm.js";
import { api } from "./api.js";
import { h, clear, fmtCount } from "./util.js";

const COLORS = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2",
  "#4b5563", "#db2777", "#65a30d", "#7c3aed"];
const LEVEL_TEXT = { 0: "raw", 1: "1:16", 2: "1:256" };
const HEIGHT = 280;

export function isResidual(meta) {
  return /resid|initial residual/i.test(meta.name) || /residual/i.test(meta.file_path || "");
}

export class Plot {
  /**
   * @param {HTMLElement} container
   * @param {{title: string, series: Array<{meta: object, label: string}>, time?: boolean,
   *          log?: boolean, percent?: boolean}} options
   */
  constructor(container, options) {
    this.options = options;
    this.series = options.series.map((s, i) => ({
      meta: s.meta, label: s.label, color: COLORS[i % COLORS.length],
      x: [], y: [], tailX: null, level: 0, shown: 0, total: s.meta.point_count,
    }));
    this.log = !!options.log;
    this.window = null; // [min, max] when zoomed
    this.uplot = null;
    this.renderPending = false;

    this.levelEl = h("span", { class: "level small muted" });
    this.logBtn = h("button", { onclick: () => this.toggleLog() }, "log y");
    this.resetBtn = h("button", { onclick: () => this.resetZoom(), disabled: true }, "Reset zoom");
    this.chartEl = h("div", { class: "chart" });
    this.root = h("div", { class: "plot" },
      h("div", { class: "plot-head" },
        h("span", { class: "title" }, options.title),
        options.percent ? null : this.logBtn, this.resetBtn, this.levelEl),
      this.chartEl);
    container.append(this.root);
    this.logBtn.classList.toggle("on", this.log);
    this.onResize = () => this.uplot && this.uplot.setSize({ width: this.width(), height: HEIGHT });
    window.addEventListener("resize", this.onResize);
  }

  width() {
    return Math.max(300, this.root.clientWidth || 800);
  }

  seriesIds() {
    return this.series.map((s) => s.meta.id);
  }

  has(seriesId) {
    return this.series.some((s) => s.meta.id === seriesId);
  }

  /** Last x received for a series: what the server must send after. */
  tailX(seriesId) {
    const s = this.series.find((t) => t.meta.id === seriesId);
    return s ? s.tailX : null;
  }

  async load() {
    const params = { width: this.width() };
    if (this.window) [params.x_min, params.x_max] = this.window;
    const results = await Promise.all(this.series.map((s) => api.points(s.meta.id, params)));
    results.forEach((r, i) => {
      const s = this.series[i];
      s.x = r.x; s.y = r.y; s.level = r.level; s.shown = r.x.length; s.total = r.total_points;
      if (!this.window) s.tailX = r.x.length ? r.x[r.x.length - 1] : null;
    });
    this.render(true);
  }

  appendPoints(seriesId, xs, ys) {
    const s = this.series.find((t) => t.meta.id === seriesId);
    if (!s || !xs.length) return;
    for (let i = 0; i < xs.length; i++) {
      if (s.tailX !== null && xs[i] <= s.tailX) continue;
      s.x.push(xs[i]); s.y.push(ys[i]); s.tailX = xs[i];
    }
    s.shown = s.x.length; s.total += xs.length;
    if (!this.renderPending) {
      this.renderPending = true;
      requestAnimationFrame(() => { this.renderPending = false; this.render(false); });
    }
  }

  data() {
    const prep = (ys) => (this.log ? ys.map((v) => (v > 0 ? v : null)) : ys);
    if (this.series.length === 1) return [this.series[0].x, prep(this.series[0].y)];
    const joined = uPlot.join(this.series.map((s) => [s.x, s.y]));
    return [joined[0], ...joined.slice(1).map(prep)];
  }

  render(rescale) {
    const data = this.data();
    this.updateLevel();
    if (this.uplot) {
      this.uplot.setData(data, rescale && !this.window);
      if (this.window) this.uplot.setScale("x", { min: this.window[0], max: this.window[1] });
      return;
    }
    const opts = {
      width: this.width(),
      height: HEIGHT,
      scales: {
        x: { time: !!this.options.time },
        y: this.log ? { distr: 3 } : (this.options.percent ? { range: [0, 1] } : {}),
      },
      axes: [
        {},
        this.options.percent
          ? { values: (u, ticks) => ticks.map((t) => `${Math.round(t * 100)}%`) }
          : { size: 70 },
      ],
      series: [
        { label: this.options.xLabel || "x" },
        ...this.series.map((s) => ({ label: s.label, stroke: s.color, width: 1.25, spanGaps: true })),
      ],
      cursor: { drag: { x: true, y: false, setScale: false } },
      hooks: { setSelect: [(u) => this.onSelect(u)] },
      legend: { live: true },
    };
    clear(this.chartEl);
    this.uplot = new uPlot(opts, data, this.chartEl);
  }

  onSelect(u) {
    if (u.select.width < 5) return;
    const min = u.posToVal(u.select.left, "x");
    const max = u.posToVal(u.select.left + u.select.width, "x");
    u.setSelect({ left: 0, width: 0, top: 0, height: 0 }, false);
    this.window = [min, max];
    this.resetBtn.disabled = false;
    this.load().catch((err) => console.error(err));
  }

  resetZoom() {
    this.window = null;
    this.resetBtn.disabled = true;
    this.load().catch((err) => console.error(err));
  }

  toggleLog() {
    this.log = !this.log;
    this.logBtn.classList.toggle("on", this.log);
    if (this.uplot) { this.uplot.destroy(); this.uplot = null; }
    this.render(true);
  }

  updateLevel() {
    const level = Math.max(...this.series.map((s) => s.level));
    const shown = this.series.reduce((a, s) => a + s.shown, 0);
    const total = this.series.reduce((a, s) => a + s.total, 0);
    const reduced = level > 0 || shown < total;
    this.levelEl.textContent = `${LEVEL_TEXT[level] || level} · ${fmtCount(shown)}${reduced ? ` / ${fmtCount(total)}` : ""} points`;
  }

  destroy() {
    window.removeEventListener("resize", this.onResize);
    if (this.uplot) this.uplot.destroy();
    this.root.remove();
  }
}
