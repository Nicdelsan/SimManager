// Authentication prompts (password, OTP, key passphrase, yes/no) in a modal dialog.
// Answers go only over the WebSocket; the fields are cleared as soon as they are sent.

import { h, clear } from "./util.js";
import * as ws from "./ws.js";

const queue = [];
let current = null;

const dialog = () => document.getElementById("auth-dialog");

export function pendingCount() {
  return queue.length + (current ? 1 : 0);
}

export function install(onChange) {
  ws.on("auth.prompt", (m) => enqueue(m, onChange));
  ws.on("auth.confirm", (m) => enqueue(m, onChange));
  ws.on("auth.closed", (m) => {
    const i = queue.findIndex((p) => p.prompt_id === m.prompt_id);
    if (i >= 0) queue.splice(i, 1);
    if (current && current.prompt_id === m.prompt_id) close();
    onChange();
  });
  document.getElementById("auth-form").addEventListener("submit", (event) => {
    event.preventDefault();
    answer();
    onChange();
  });
  document.getElementById("auth-cancel").addEventListener("click", () => {
    if (current) ws.send({ type: "auth.cancel", prompt_id: current.prompt_id });
    close();
    onChange();
  });
  dialog().addEventListener("cancel", (event) => event.preventDefault()); // Esc: use Cancel
}

function enqueue(message, onChange) {
  if (current && current.prompt_id === message.prompt_id) return;
  if (queue.some((p) => p.prompt_id === message.prompt_id)) return;
  queue.push(message);
  if (!current) showNext();
  onChange();
}

function showNext() {
  current = queue.shift() || null;
  if (!current) return;
  const fields = document.getElementById("auth-fields");
  document.getElementById("auth-title").textContent = current.type === "auth.confirm"
    ? `${current.cluster_id}: confirmation`
    : current.title;
  document.getElementById("auth-instructions").textContent =
    current.type === "auth.confirm" ? current.question : (current.instructions || "");
  if (current.type === "auth.confirm") {
    clear(fields);
    document.getElementById("auth-ok").textContent = "Yes";
    document.getElementById("auth-cancel").textContent = "No";
  } else {
    clear(fields, current.prompts.map((p, i) =>
      h("label", { class: "field" },
        h("span", {}, p.text),
        h("input", {
          type: p.echo ? "text" : "password",
          name: `answer-${i}`,
          autocomplete: "off",
          autofocus: i === 0,
        }))));
    document.getElementById("auth-ok").textContent = "OK";
    document.getElementById("auth-cancel").textContent = "Cancel";
  }
  if (!dialog().open) dialog().showModal();
  const first = fields.querySelector("input");
  if (first) first.focus();
}

function answer() {
  if (!current) return;
  if (current.type === "auth.confirm") {
    ws.send({ type: "auth.confirm_answer", prompt_id: current.prompt_id, yes: true });
  } else {
    const inputs = [...document.querySelectorAll("#auth-fields input")];
    ws.send({ type: "auth.answer", prompt_id: current.prompt_id, values: inputs.map((i) => i.value) });
  }
  close();
}

function close() {
  for (const input of document.querySelectorAll("#auth-fields input")) input.value = "";
  clear(document.getElementById("auth-fields"));
  current = null;
  if (queue.length) showNext();
  else if (dialog().open) dialog().close();
}
