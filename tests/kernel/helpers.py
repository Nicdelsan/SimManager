"""Small plugin factories that record what happens into a shared journal."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from simmanager.kernel import Context, NoConfig, Plugin


class Service:
    def __init__(self, owner: str) -> None:
        self.owner = owner


def make_plugin(
    name: str,
    journal: list[str],
    *,
    provides: str | None = None,
    inject: Mapping[str, int] | None = None,
    version: int = 1,
    effects: int = 0,
    async_effects: bool = False,
    fail_on_apply: bool = False,
    async_apply: bool = False,
    schema: type[BaseModel] = NoConfig,
) -> type[Plugin]:
    """Plugin that registers `effects` disposers (journaled as 'dispose <name>:<i>')."""

    def setup(ctx: Context) -> Service | None:
        journal.append(f"apply {name}")
        for i in range(effects):
            if async_effects and i % 2:

                async def adispose(i: int = i) -> None:
                    await asyncio.sleep(0)
                    journal.append(f"dispose {name}:{i}")

                ctx.add_disposer(adispose)
            else:
                ctx.effect(lambda i=i: lambda: journal.append(f"dispose {name}:{i}"))
        if fail_on_apply:
            raise RuntimeError(f"{name} exploded")
        return Service(name) if provides else None

    if async_apply:

        async def apply(self: Plugin, ctx: Context) -> Any:
            await asyncio.sleep(0)
            return setup(ctx)
    else:

        def apply(self: Plugin, ctx: Context) -> Any:
            return setup(ctx)

    attrs: dict[str, Any] = {
        "name": name,
        "provides": provides,
        "contract_version": version,
        "inject": dict(inject or {}),
        "config_schema": schema,
        "apply": apply,
    }
    return type(f"P_{name.replace('-', '_')}", (Plugin,), attrs)
