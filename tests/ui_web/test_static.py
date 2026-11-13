"""The frontend is served correctly: every ES module import resolves, with JS MIME types
(required by `nosniff`), and pages contain no inline scripts or styles (CSP)."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from simmanager.plugins.ui_web.app import STATIC_DIR

from .webclient import running_web

IMPORT_RE = re.compile(r"""^\s*import\s[^'"]*["']([^"']+)["']""", re.MULTILINE)


def _modules() -> list[str]:
    return sorted(
        "/static/" + p.relative_to(STATIC_DIR).as_posix()
        for p in STATIC_DIR.rglob("*.js")
        if "vendor" not in p.parts
    )


async def test_modules_and_imports_are_served_as_javascript() -> None:
    async with running_web() as web, web.client() as c:
        seen: set[str] = set()
        todo = ["/static/js/main.js"]
        while todo:
            url = todo.pop()
            if url in seen:
                continue
            seen.add(url)
            r = await c.get(url)
            assert r.status_code == 200, url
            assert r.headers["content-type"].startswith("text/javascript"), url
            for spec in IMPORT_RE.findall(r.text):
                assert spec.startswith("."), (url, spec)  # no bare or remote imports
                todo.append(posixpath.normpath(posixpath.join(posixpath.dirname(url), spec)))
        assert set(_modules()) <= seen, set(_modules()) - seen  # no orphan module
        css = await c.get("/static/css/app.css")
        assert css.headers["content-type"].startswith("text/css")


def test_index_has_no_inline_script_or_style() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<style" not in html and " style=" not in html
    for tag in re.findall(r"<script[^>]*>", html):
        assert "src=" in tag, tag
    assert re.search(r"<script[^>]*>\s*[^<\s]", html) is None  # empty bodies only


def test_frontend_never_uses_inner_html() -> None:
    for path in Path(STATIC_DIR, "js").rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert "innerHTML" not in text and "insertAdjacentHTML" not in text, path
        assert 'setAttribute("style"' not in text, path
