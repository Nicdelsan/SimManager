// Dashboard WebSocket with automatic reconnection and a tiny pub/sub by message type.

const handlers = new Map();
const openHandlers = new Set();
let socket = null;
let retryMs = 1000;
let statusCallback = () => {};

export function on(type, handler) {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type).add(handler);
  return () => handlers.get(type).delete(handler);
}

/** Run now if connected and after every (re)connection: used to (re)subscribe. */
export function onOpen(handler) {
  openHandlers.add(handler);
  if (socket && socket.readyState === WebSocket.OPEN) handler();
  return () => openHandlers.delete(handler);
}

export function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
    return true;
  }
  return false;
}

export function onStatus(callback) {
  statusCallback = callback;
}

export function connect() {
  socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket.addEventListener("open", () => {
    retryMs = 1000;
    statusCallback(true);
    for (const handler of openHandlers) handler();
  });
  socket.addEventListener("message", (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    for (const handler of handlers.get(message.type) || []) {
      try { handler(message); } catch (err) { console.error(err); }
    }
    for (const handler of handlers.get("*") || []) handler(message);
  });
  socket.addEventListener("close", () => {
    statusCallback(false);
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 15000);
  });
}
