"""Security of the local web server: token/cookie, Host, Origin, JSON-only, headers."""

from __future__ import annotations

import logging

import pytest

from simmanager.plugins.ui_web.security import SecurityPolicy

from .webclient import Web, running_web, ws_handshake_status

EXPECTED_HEADERS = {
    "content-security-policy": "default-src 'self'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


def _assert_security_headers(headers: object) -> None:
    for name, value in EXPECTED_HEADERS.items():
        assert headers[name] == value, name  # type: ignore[index]


# --------------------------------------------------------------------- policy unit
def test_policy_host_origin_token() -> None:
    p = SecurityPolicy(token="secret-token", port=8765)
    assert p.host_ok("127.0.0.1:8765") and p.host_ok("LOCALHOST:8765")
    for bad in (None, "", "127.0.0.1", "127.0.0.1:8766", "evil.example:8765", "0.0.0.0:8765"):
        assert not p.host_ok(bad), bad
    assert p.origin_ok("http://127.0.0.1:8765") and p.origin_ok("http://localhost:8765")
    for bad in (None, "null", "http://evil.example", "https://127.0.0.1:8765", "http://127.0.0.1"):
        assert not p.origin_ok(bad), bad
    assert p.token_ok("secret-token")
    assert not p.token_ok(None) and not p.token_ok("") and not p.token_ok("secret-tokem")
    cookie = p.set_cookie_header()
    assert cookie.startswith("simman_session_8765=secret-token;")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie


# ------------------------------------------------------------------ real server
@pytest.fixture
async def web() -> object:
    async with running_web() as w:
        yield w


async def test_server_listens_on_loopback_only(web: Web) -> None:
    assert web.server.base_url == f"http://127.0.0.1:{web.port}/"
    assert web.server.policy.token not in web.server.base_url


async def test_everything_requires_the_session_cookie(web: Web) -> None:
    async with web.client(authenticated=False) as c:
        for path in ("/", "/api/health", "/static/index.html", "/api/nothing"):
            r = await c.get(path)
            assert r.status_code == 401, path
            _assert_security_headers(r.headers)
        r = await c.get("/api/health", headers={"Cookie": f"{web.server.policy.cookie_name}=x"})
        assert r.status_code == 401
    async with web.client() as c:
        for path in ("/", "/api/health", "/static/index.html"):
            r = await c.get(path)
            assert r.status_code == 200, path
            _assert_security_headers(r.headers)


async def test_token_login_sets_strict_cookie_and_redirects(web: Web) -> None:
    async with web.client(authenticated=False) as c:
        bad = await c.get("/", params={"token": "wrong"})
        assert bad.status_code == 401 and "set-cookie" not in bad.headers
        r = await c.get("/", params={"token": web.server.policy.token})
        assert r.status_code == 303 and r.headers["location"] == "/"
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{web.server.policy.cookie_name}=")
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        # The browser now holds the cookie; the token is gone from the URL.
        page = await c.get("/", headers={"Cookie": cookie.split(";", 1)[0]})
        assert page.status_code == 200 and "SimManager" in page.text


async def test_foreign_host_is_rejected(web: Web) -> None:
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends Host: evil.example."""
    async with web.client() as c:
        r = await c.get("/api/health", headers={"Host": f"evil.example:{web.port}"})
        assert r.status_code == 400
        r = await c.get("/api/health", headers={"Host": f"localhost:{web.port}"})
        assert r.status_code == 200


async def test_state_changing_requests_need_origin_and_json(web: Web) -> None:
    async with web.client() as c:
        no_origin = await c.post("/api/health", json={})
        assert no_origin.status_code == 403
        foreign = await c.post("/api/health", json={}, headers={"Origin": "http://evil.example"})
        assert foreign.status_code == 403
        form = await c.post(
            "/api/health",
            content=b"a=1",
            headers={"Origin": web.origin, "Content-Type": "application/x-www-form-urlencoded"},
        )
        assert form.status_code == 415
        # Passes the security layer; the route itself only allows GET.
        ok = await c.post("/api/health", json={}, headers={"Origin": web.origin})
        assert ok.status_code == 405
    async with web.client(authenticated=False) as c:
        r = await c.post("/api/health", json={}, headers={"Origin": web.origin})
        assert r.status_code == 401


async def test_websocket_handshake_checks(web: Web) -> None:
    good = [("Origin", web.origin), ("Cookie", web.cookie)]
    assert await ws_handshake_status(web.port, headers=good) == 101
    assert await ws_handshake_status(web.port, headers=[("Origin", web.origin)]) == 403
    assert await ws_handshake_status(web.port, headers=[("Cookie", web.cookie)]) == 403
    foreign = [("Origin", "http://evil.example"), ("Cookie", web.cookie)]
    assert await ws_handshake_status(web.port, headers=foreign) == 403
    rebinding = await ws_handshake_status(web.port, host=f"evil.example:{web.port}", headers=good)
    assert rebinding == 403


async def test_token_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    async with running_web() as web:
        async with web.client(authenticated=False) as c:
            await c.get("/", params={"token": web.server.policy.token})
            await c.get("/", params={"token": "wrong"})
        async with web.client() as c:
            await c.get("/api/health")
    # Server side only: the test's own HTTP client (httpx/httpcore) logs its request URLs.
    server_side = [
        r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))
    ]
    assert any(m for m in server_side)  # uvicorn/kernel did log something
    assert all(web.server.policy.token not in m for m in server_side)
