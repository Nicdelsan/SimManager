"""ui-web server lifecycle and the `ui.web` service (broadcast, messages, Ctrl+C)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from simmanager.contracts.ui import Message
from simmanager.kernel import Kernel, PluginState
from simmanager.plugins.ui_web import UiWebPlugin

from .webclient import recv_json, running_web, ws_connect


async def test_hello_broadcast_and_messages() -> None:
    async with running_web() as web:
        received: list[Message] = []

        async def on_ping(message: Message) -> None:
            received.append(message)

        async def on_connect() -> list[Message]:
            return [{"type": "pending", "n": 1}]

        web.server.on_message("ping", on_ping)
        web.server.on_client_connected(on_connect)
        async with ws_connect(web) as ws:
            assert (await recv_json(ws))["type"] == "hello"
            assert await recv_json(ws) == {"type": "pending", "n": 1}
            assert web.server.client_count() == 1
            await web.server.broadcast({"type": "news", "x": [1.5]})
            assert await recv_json(ws) == {"type": "news", "x": [1.5]}
            await ws.send("not json")
            await ws.send(json.dumps({"type": "ping", "v": 2}))
            for _ in range(100):
                if received:
                    break
                await asyncio.sleep(0.01)
            assert received == [{"type": "ping", "v": 2}]
        for _ in range(100):
            if web.server.client_count() == 0:
                break
            await asyncio.sleep(0.01)
        assert web.server.client_count() == 0


async def test_stop_closes_port_and_open_websockets() -> None:
    async with running_web() as web:
        port = web.port
        ws = await ws_connect(web)
        await recv_json(ws)
        await web.kernel.stop()
        await asyncio.wait_for(ws.wait_closed(), 10)
    with pytest.raises(OSError):
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()


async def test_busy_port_fails_with_clear_error() -> None:
    async with running_web() as web:
        kernel = Kernel({"ui-web": {"port": web.port}})
        try:
            await kernel.add(UiWebPlugin)
            info = next(i for i in kernel.info() if i.name == "ui-web")
            assert info.state is PluginState.FAILED
            assert info.error is not None and "--port" in info.error
        finally:
            await kernel.stop()


_CTRL_C_CHILD = textwrap.dedent(
    """
    import signal, sys, threading, time
    from simmanager.cli.main import app

    def press_ctrl_c():
        time.sleep(float(sys.argv[1]))
        signal.raise_signal(signal.SIGINT)

    threading.Thread(target=press_ctrl_c, daemon=True).start()
    try:
        app(["serve", "--no-browser", "--port", "0"])
    except SystemExit as exc:
        sys.exit(exc.code)
    """
)


def test_serve_stops_cleanly_on_ctrl_c(tmp_path: Path) -> None:
    """Real `simman serve` in a child process; SIGINT is what Ctrl+C sends (also on Windows)."""
    script = tmp_path / "child.py"
    script.write_text(_CTRL_C_CHILD, encoding="utf-8")
    env = {
        **os.environ,
        "SIMMANAGER_CONFIG_DIR": str(tmp_path / "config"),
        "SIMMANAGER_DATA_DIR": str(tmp_path / "data"),
        "PYTHONIOENCODING": "utf-8",
        "COLUMNS": "200",
    }
    proc = subprocess.run(
        [sys.executable, str(script), "4"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "Web UI: http://127.0.0.1:" in out and "?token=" in out
    assert "stopped." in out
    assert "Traceback" not in out
