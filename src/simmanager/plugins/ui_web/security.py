"""Security of the local web server (ADR 0005).

The server listens on 127.0.0.1 only, but any web page open in the user's browser can
send requests to it. Defences:

- a random token per start, printed by `simman serve`, exchanged once for an
  `HttpOnly; SameSite=Strict` cookie; every page, asset, API call and WebSocket needs it;
- Host check against DNS rebinding (only `127.0.0.1:<port>` / `localhost:<port>`);
- Origin required and checked on WebSockets and state-changing requests (cross-site
  WebSocket hijacking, CSRF); state-changing requests must be `application/json`;
- `Content-Security-Policy: default-src 'self'`, `nosniff`, `Referrer-Policy: no-referrer`.
  The terminal page (`/terminal`) alone also allows inline styles: xterm.js creates
  `<style>` elements at runtime. Scripts stay `'self'` everywhere.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOOPBACK = "127.0.0.1"
TOKEN_PARAM = "token"
CSP = "default-src 'self'; frame-ancestors 'none'"
TERMINAL_PAGE = "/terminal"
TERMINAL_CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)
_LOGIN_HINT = (
    "<!doctype html><meta charset=utf-8><title>SimManager</title>"
    "<p>Not authenticated. Open the link printed by <code>simman serve</code>.</p>"
)


def new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class SecurityPolicy:
    token: str
    port: int

    @property
    def cookie_name(self) -> str:
        # Cookies ignore ports: one name per port keeps two servers apart.
        return f"simman_session_{self.port}"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({f"{LOOPBACK}:{self.port}", f"localhost:{self.port}"})

    def host_ok(self, host: str | None) -> bool:
        return host is not None and host.lower() in self.allowed_hosts

    def origin_ok(self, origin: str | None) -> bool:
        return origin is not None and origin.lower() in {f"http://{h}" for h in self.allowed_hosts}

    def token_ok(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        return secrets.compare_digest(candidate.encode(), self.token.encode())

    def set_cookie_header(self) -> str:
        return f"{self.cookie_name}={self.token}; HttpOnly; SameSite=Strict; Path=/"


def _media_type(headers: Headers) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


class SecurityMiddleware:
    """Pure ASGI middleware: covers HTTP and WebSocket scopes alike."""

    def __init__(self, app: ASGIApp, policy: SecurityPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if scope["type"] == "websocket":
            if (
                self.policy.host_ok(headers.get("host"))
                and self.policy.origin_ok(headers.get("origin"))
                and self._authenticated(headers)
            ):
                await self.app(scope, receive, send)
            else:
                # Closing before accept makes the server answer the handshake with 403.
                await send({"type": "websocket.close", "code": 1008})
            return

        if not self.policy.host_ok(headers.get("host")):
            await _respond(send, 400, "invalid Host header")
            return
        method = scope["method"]
        if method in UNSAFE_METHODS:
            if not self.policy.origin_ok(headers.get("origin")):
                await _respond(send, 403, "missing or foreign Origin")
                return
            if _media_type(headers) != "application/json":
                await _respond(send, 415, "requests must be application/json")
                return

        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if scope["path"] == "/" and method == "GET" and TOKEN_PARAM in query:
            if self.policy.token_ok(query[TOKEN_PARAM][0]):
                # Token -> cookie, then drop the token from the address bar and history.
                await _respond(
                    send,
                    303,
                    "",
                    extra=[("Location", "/"), ("Set-Cookie", self.policy.set_cookie_header())],
                )
            else:
                await _respond(send, 401, _LOGIN_HINT, html=True)
            return
        if not self._authenticated(headers):
            await _respond(send, 401, _LOGIN_HINT, html=True)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    out[name] = value
                if scope["path"] == TERMINAL_PAGE:
                    out["Content-Security-Policy"] = TERMINAL_CSP
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _authenticated(self, headers: Headers) -> bool:
        cookies = cookie_parser(headers.get("cookie", ""))
        return self.policy.token_ok(cookies.get(self.policy.cookie_name))


async def _respond(
    send: Send,
    status: int,
    body: str,
    *,
    html: bool = False,
    extra: list[tuple[str, str]] | None = None,
) -> None:
    content = body.encode()
    content_type = "text/html; charset=utf-8" if html else "text/plain; charset=utf-8"
    raw = [
        (b"content-type", content_type.encode()),
        (b"content-length", str(len(content)).encode()),
        *((k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS),
        *((k.lower().encode(), v.encode()) for k, v in extra or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": content})
