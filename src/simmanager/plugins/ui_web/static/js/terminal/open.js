// Open a terminal from any page: the terminal window ("simman-terminal") is reused and
// gets a new tab. Loaded by the dashboard pages; the terminal page itself reads the hash.

export function openTerminal({ cluster, cwd = null, run = null }) {
  const q = new URLSearchParams({ cluster });
  if (cwd) q.set("cwd", cwd);
  if (run) q.set("run", String(run));
  const win = window.open(`/terminal#new?${q}`, "simman-terminal");
  if (win) win.focus();
  else alert("The browser blocked the terminal window: allow pop-ups for SimManager.");
}
