import { api } from "../api.js";
import { h, clear, debounce } from "../util.js";
import { clusterCard, eventItem, listen } from "./common.js";

export async function clustersView(main) {
  const cardsEl = h("div", { class: "clusters" });
  const eventsEl = h("ul", { class: "events" });
  clear(main,
    h("div", { class: "panel" },
      h("h2", {}, "Clusters"),
      h("p", { class: "small muted" },
        "Connections are opened by the monitor. When a cluster needs a password or an OTP code, ",
        "a dialog appears here; if nobody answers within the timeout the cluster waits for ",
        "\"Reconnect\" (no blind retries)."),
      cardsEl),
    h("div", { class: "panel" }, h("h2", {}, "Connection events"), eventsEl));

  const load = async () => {
    const clusters = await api.clusters();
    clear(cardsEl, clusters.length
      ? clusters.map((c) => clusterCard(c, { onReconnect: () => refresh() }))
      : h("p", { class: "muted" }, "No clusters configured: `simman cluster add`."));
  };
  const loadEvents = async () => {
    const events = (await api.events({ limit: 300 })).filter((e) => e.kind.startsWith("cluster."));
    clear(eventsEl, events.slice(0, 60).map(eventItem));
  };
  await Promise.all([load(), loadEvents()]);
  const refresh = debounce(() => load().catch(() => {}), 300);
  return listen({
    "cluster.state": refresh,
    "monitor.polled": refresh,
    event: (e) => { if (e.kind.startsWith("cluster.")) eventsEl.prepend(eventItem(e)); },
  });
}
