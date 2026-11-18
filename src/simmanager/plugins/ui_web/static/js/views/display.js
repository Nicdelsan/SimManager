// Display panel of the run detail: images made by Python scripts run on the cluster
// (left: latest image, history, folder browser) and the script editor (right, display_editor.js).

import { api, displayUrls, probe } from "../api.js";
import { h, clear, debounce, fmtTime, stateBadge } from "../util.js";
import { listen } from "./common.js";
import { scriptEditor } from "./display_editor.js";

const STATUS_BADGE = { running: "pending", ok: "completed", failed: "failed", error: "failed", interrupted: "stopped" };

export function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} kB`;
  return `${n} B`;
}

function execStatus(e) {
  return stateBadge(STATUS_BADGE[e.status] || "", e.status);
}

function execLabel(e) {
  return `${fmtTime(e.started_at)} · ${e.script_name} v${e.version_number}${e.trigger === "auto" ? " · auto" : ""}`;
}

function linkTo(text, onclick) {
  return h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); onclick(); } }, text);
}

/** Shows `url` in an <img>; on error asks the server why (e.g. 413 too large). */
function imageBox(url, { caption, onTooLarge } = {}) {
  const note = h("div", { class: "small" });
  const img = h("img", { class: "display-img", alt: caption || "", src: url });
  img.addEventListener("error", async () => {
    img.hidden = true;
    const result = await probe(url).catch(() => ({ ok: false, status: 0 }));
    const d = result.detail;
    if (result.status === 413 && d && onTooLarge) {
      clear(note, h("span", { class: "muted" }, `Image too large: ${fmtBytes(d.size)} (limit ${fmtBytes(d.limit)}). `),
        h("button", { type: "button", onclick: onTooLarge }, "Open anyway"));
    } else {
      clear(note, h("span", { class: "error" }, typeof d === "string" ? d : `cannot load the image (HTTP ${result.status})`));
    }
  });
  return h("figure", { class: "display-figure" }, img, note,
    caption ? h("figcaption", { class: "small muted mono" }, caption) : null);
}

function skippedImage(img, refresh) {
  const why = img.skipped === "too_large" ? `too large (${fmtBytes(img.size)})`
    : img.skipped === "limit" ? "not copied (more new images than display.max_images_per_execution)"
    : `not copied: ${img.detail}`;
  const button = h("button", { type: "button", onclick: async () => {
    button.disabled = true;
    try { await api.fetchDisplayImage(img.id); await refresh(); }
    catch (err) { alert(err.message); button.disabled = false; }
  } }, "Download anyway");
  return h("p", { class: "small" }, h("span", { class: "mono" }, img.path), " — ", why, " ", button);
}

export function displayPanel(runId) {
  const viewEl = h("div", {});
  const tabs = h("div", { class: "toolbar" });
  let data = { executions: [], schedules: [], busy: false, max_image_bytes: 0 };
  let tab = "latest";
  let selectedExecution = null; // null: newest execution with images
  let selectedImage = null;
  let folder = "";
  let preview = null; // {path, force}

  const refresh = async () => {
    try {
      data = await api.runDisplay(runId);
    } catch (err) {
      clear(viewEl, h("p", { class: "muted" }, `Display not available: ${err.message}`));
      return;
    }
    if (tab === "files") { renderTabs(); editor.setSchedules(data.schedules, data.busy); } // keep the browser as it is
    else render();
  };
  const editor = scriptEditor(runId, { onStarted: () => refresh() });
  const root = h("div", { class: "panel display" }, h("h2", {}, "Display"),
    h("div", { class: "row" }, h("div", { class: "grow display-view" }, tabs, viewEl), editor.el));

  const renderTabs = () => {
    const button = (key, label) => h("button", {
      type: "button", class: tab === key ? "on" : "", onclick: () => { tab = key; render(); },
    }, label);
    clear(tabs, button("latest", "Image"), button("history", `History (${data.executions.length})`), button("files", "Files"),
      data.busy ? h("span", { class: "small muted" }, "⏳ script running on the cluster…") : null);
  };

  const shownExecution = () => {
    if (selectedExecution !== null) return data.executions.find((e) => e.id === selectedExecution) || null;
    return data.executions.find((e) => e.images.some((i) => i.stored)) || data.executions[0] || null;
  };

  const renderLatest = () => {
    const e = shownExecution();
    if (!e) return h("p", { class: "muted" }, "No images yet: write or pick a script on the right and press Run.");
    const images = e.images;
    const current = images.find((i) => i.id === selectedImage) || images.find((i) => i.stored) || images[0];
    return [
      h("div", { class: "small" }, execStatus(e), " ", execLabel(e), images.length ? ` · ${images.length} image(s)` : ""),
      e.error ? h("p", { class: "error small" }, e.error) : null,
      !current ? h("p", { class: "muted small" }, "The script wrote no new image (png, gif, jpg, webp) in the run folder.")
        : current.stored ? imageBox(displayUrls.image(current.id), { caption: `${current.path} · ${fmtTime(current.mtime)}` })
        : skippedImage(current, refresh),
      images.length > 1
        ? h("div", { class: "display-thumbs" }, images.map((i) => h("button", {
          type: "button", class: i === current ? "on" : "", title: i.path,
          onclick: () => { selectedImage = i.id; render(); },
        }, i.path.split("/").pop())))
        : null,
      e.stdout || e.stderr
        ? h("details", { class: "small" }, h("summary", {}, "Script output"),
          h("pre", { class: "display-out" }, e.stdout || ""),
          e.stderr ? h("pre", { class: "display-out error" }, e.stderr) : null)
        : null,
    ];
  };

  const renderHistory = () => {
    if (!data.executions.length) return h("p", { class: "muted" }, "No executions yet.");
    return h("table", { class: "small" },
      h("tr", {}, h("th", {}, "When"), h("th", {}, "Script"), h("th", {}, "Status"), h("th", {}, "Images")),
      data.executions.map((e) => h("tr", {
        class: "clickable", title: "Show these images",
        onclick: () => { selectedExecution = e.id; selectedImage = null; tab = "latest"; render(); },
      },
      h("td", {}, fmtTime(e.started_at), e.trigger === "auto" ? h("span", { class: "muted" }, " auto") : null),
      h("td", {}, `${e.script_name} v${e.version_number}`),
      h("td", {}, execStatus(e)),
      h("td", {}, String(e.images.filter((i) => i.stored).length)))));
  };

  const openFolder = (path) => { folder = path; preview = null; render(); };

  const renderFiles = () => {
    const crumbs = [linkTo("run folder", () => openFolder(""))];
    let acc = "";
    for (const part of folder.split("/").filter(Boolean)) {
      acc = acc ? `${acc}/${part}` : part;
      const target = acc;
      crumbs.push(" / ", linkTo(part, () => openFolder(target)));
    }
    const list = h("ul", { class: "display-files small" }, h("li", { class: "muted" }, "loading…"));
    api.displayFiles(runId, folder).then((entries) => clear(list,
      entries.length ? null : h("li", { class: "muted" }, "empty folder"),
      entries.map((e) => h("li", {},
        e.is_dir ? linkTo(`📁 ${e.name}`, () => openFolder(e.path))
          : e.media_type ? linkTo(`🖼 ${e.name}`, () => { preview = { path: e.path, force: false }; render(); })
          : h("span", { class: "muted" }, e.name),
        e.is_dir ? null : h("span", { class: "muted" }, ` ${fmtBytes(e.size)} · ${fmtTime(e.mtime)}`)))))
      .catch((err) => clear(list, h("li", { class: "error" }, err.message)));
    return [
      h("div", { class: "small mono" }, crumbs),
      h("div", { class: "row" }, h("div", { class: "display-browser" }, list),
        h("div", { class: "grow" }, preview
          ? imageBox(displayUrls.file(runId, preview.path, preview.force), {
            caption: preview.path,
            onTooLarge: () => { preview = { ...preview, force: true }; render(); },
          })
          : h("p", { class: "muted small" }, "Pick an image to show it."))),
    ];
  };

  function render() {
    renderTabs();
    clear(viewEl, tab === "history" ? renderHistory() : tab === "files" ? renderFiles() : renderLatest());
    editor.setSchedules(data.schedules, data.busy);
  }

  const refreshSoon = debounce(refresh, 300);
  const off = listen({
    "display.updated": (m) => {
      if (m.run_id !== runId) return;
      if (m.status !== "running") { selectedExecution = null; selectedImage = null; }
      refreshSoon();
    },
  });

  editor.load();
  refresh();
  return { el: root, destroy: off };
}
