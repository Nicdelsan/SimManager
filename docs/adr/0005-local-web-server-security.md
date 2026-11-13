# ADR 0005 — Local web UI: in-process server and its security

- Status: accepted (M3, 2026-11-13)

## Context

M3 adds a browser UI. It must use the kernel's live services (storage, transport, the
SSH authentication prompts) without a second process, work offline, and need no frontend
toolchain. A server on 127.0.0.1 is still reachable by every web page the user opens:
a malicious page can send requests to `http://127.0.0.1:<port>` (CSRF), open WebSockets to
it (cross-site WebSocket hijacking) or point its own domain at 127.0.0.1 (DNS rebinding).
Through this UI the user answers passwords and OTP codes for HPC accounts.

## Decision

1. **In-process server.** Plugin `ui-web` runs uvicorn + FastAPI inside the kernel's event
   loop (`uvicorn.Server.serve` as a task; uvicorn's own signal capture is disabled so
   Ctrl+C stays with `simman serve`, which stops the kernel and thus the server). Only
   `simman serve` enables it (`WEB_ONLY_PLUGINS`). Starlette's `TestClient` is not used in
   tests (it runs the app in another loop); tests start the real server on port 0.
2. **Loopback only.** The socket is bound to `127.0.0.1` by the plugin itself; the address
   is not configurable, the port is (`ui-web.port`, `--port`, default 8765). A busy port is
   a clear error, never a silent switch.
3. **Session token.** `secrets.token_urlsafe(32)` per start; `serve` prints
   `http://127.0.0.1:<port>/?token=…` and opens the browser. A valid token is exchanged for
   a cookie `simman_session_<port>; HttpOnly; SameSite=Strict; Path=/` and a redirect to
   `/` removes it from the address bar. Every page, static file, API call and WebSocket
   requires the cookie (`secrets.compare_digest`). The uvicorn access log is off (the
   login URL carries the token).
4. **Host check** on HTTP and WebSocket: only `127.0.0.1:<port>` and `localhost:<port>`.
5. **Origin required** and checked on WebSocket handshakes and on state-changing methods
   (POST/PUT/PATCH/DELETE), which must also be `application/json` (no simple-form CSRF).
6. **Headers** on every response: `Content-Security-Policy: default-src 'self';
   frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy:
   no-referrer`, `Cache-Control: no-store`. No inline scripts or styles in pages; vendored
   libraries are served from `/static`.
7. **Secrets.** Answers to authentication prompts travel only over the WebSocket, are
   never logged or written to the event log, and no reference is kept after use. Hub
   errors log the message type, never the message.

## Consequences

- Each `serve` start needs the printed link (or the auto-opened browser); a restart
  invalidates open tabs, which then show the login hint.
- All security checks live in one pure ASGI middleware (`plugins/ui_web/security.py`),
  covered by tests for each rule.
- Plugins reach browsers only through the `ui.web` contract; per-session channels (the M4
  terminal) will be added as dedicated authenticated WebSocket endpoints.
