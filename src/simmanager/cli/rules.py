"""`simman rules`, `simman alerts`, and `simman notify` subcommands."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from simmanager import paths
from simmanager.app import AppSetup, prepare, start_kernel
from simmanager.contracts import campaign as campaign_contract
from simmanager.contracts import notifier as notifier_contract
from simmanager.contracts import rules as rules_contract
from simmanager.contracts.campaign import CampaignStore
from simmanager.contracts.notifier import NotificationService
from simmanager.contracts.rules import (
    RuleEngine,
    RuleKind,
    RuleScope,
    RuleSeverity,
    RuleSpec,
)
from simmanager.kernel import ConfigError, Kernel
from simmanager.logging_setup import setup_logging

rules_app = typer.Typer(no_args_is_help=True, help="Inspect and configure detection rules.")
alerts_app = typer.Typer(no_args_is_help=True, help="Inspect and acknowledge detection alerts.")
notify_app = typer.Typer(no_args_is_help=True, help="Test desktop and event notifications.")

_console = Console()
_T = TypeVar("_T")

ProjectOpt = Annotated[
    Path | None,
    typer.Option("--project", "-p", help="Project directory or simmanager.yaml file."),
]


def _prepare(project: Path | None) -> AppSetup:
    try:
        setup = prepare(project)
    except ConfigError as exc:
        _console.print(f"[red]configuration error:[/] {exc}")
        raise typer.Exit(2) from exc
    setup.core.plugins.setdefault("monitor", {})["loop"] = False
    setup_logging(paths.log_dir(), level=setup.core.log_level)
    for warning in setup.warnings:
        _console.print(f"[yellow]warning:[/] {warning}")
    return setup


def _with_kernel(setup: AppSetup, body: Callable[[Kernel], Awaitable[_T]]) -> _T:
    async def run() -> _T:
        kernel = await start_kernel(setup)
        try:
            return await body(kernel)
        finally:
            await kernel.stop()

    return asyncio.run(run())


def _require_engine(kernel: Kernel) -> RuleEngine:
    engine: RuleEngine | None = kernel.services.get(rules_contract.MANAGER_SERVICE_KEY)
    if engine is None:
        _console.print("[red]rules service unavailable[/] (see `simman plugins`)")
        raise typer.Exit(1)
    return engine


async def _resolve_scope(
    kernel: Kernel,
    campaign: str | None,
    case_key: str | None,
) -> tuple[RuleScope, int, int | None, int | None]:
    if case_key is not None and campaign is None:
        _console.print("[red]--case requires --campaign[/]")
        raise typer.Exit(2)
    if campaign is None:
        return RuleScope.GLOBAL, 0, None, None

    camp_store: CampaignStore | None = kernel.services.get(campaign_contract.SERVICE_KEY)
    if camp_store is None:
        _console.print("[red]campaign service unavailable[/]")
        raise typer.Exit(1)
    camp_id_or_name: str | int = int(campaign) if campaign.isdigit() else campaign
    camp_rec = await camp_store.campaign(camp_id_or_name)
    if camp_rec is None:
        _console.print(f"[red]campaign {campaign!r} not found[/]")
        raise typer.Exit(1)

    if case_key is None:
        return RuleScope.CAMPAIGN, camp_rec.id, camp_rec.id, None

    case_rec = await camp_store.case(camp_rec.id, case_key)
    if case_rec is None:
        _console.print(f"[red]case {case_key!r} not found in campaign {camp_rec.name}[/]")
        raise typer.Exit(1)
    return RuleScope.CASE, case_rec.id, camp_rec.id, case_rec.id


def _parse_param_value(raw: str) -> Any:
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "none":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


@rules_app.command("show")
def rules_show(
    campaign: Annotated[
        str | None, typer.Option("--campaign", "-c", help="Campaign name or id.")
    ] = None,
    case: Annotated[
        str | None, typer.Option("--case", help="Case key (requires --campaign).")
    ] = None,
    run_id: Annotated[
        int | None, typer.Option("--run", "-r", help="Resolve rules for a specific run id.")
    ] = None,
    project: ProjectOpt = None,
) -> None:
    """Show effective detection rules (global, or scoped to campaign/case/run)."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        engine = _require_engine(kernel)
        if run_id is not None:
            rules = await engine.effective_rules(run_id=run_id)
            title = f"Effective rules for run {run_id}"
        else:
            scope, _, camp_id, case_id = await _resolve_scope(kernel, campaign, case)
            rules = await engine.effective_rules(campaign_id=camp_id, case_id=case_id)
            title = f"Effective rules ({scope})"

        table = Table("key", "kind", "enabled", "severity", "scope", "params", title=title)
        for r in rules:
            table.add_row(
                r.rule_key,
                str(r.rule_kind),
                "[green]yes[/]" if r.enabled else "[dim]no[/]",
                str(r.severity),
                f"{r.origin_scope}:{r.origin_id}" if r.origin_id else str(r.origin_scope),
                json.dumps(r.params, ensure_ascii=False) if r.params else "",
            )
        _console.print(table)

    _with_kernel(setup, body)


@rules_app.command("set")
def rules_set(
    rule_key: Annotated[str, typer.Argument(help="Rule key, e.g. courant_high or monitor:p.")],
    kind: Annotated[
        RuleKind | None,
        typer.Option("--kind", "-k", help="Rule kind (inferred for built-in keys)."),
    ] = None,
    campaign: Annotated[
        str | None, typer.Option("--campaign", "-c", help="Scope to campaign name or id.")
    ] = None,
    case: Annotated[
        str | None, typer.Option("--case", help="Scope to case key (requires --campaign).")
    ] = None,
    severity: Annotated[
        RuleSeverity | None, typer.Option("--severity", "-s", help="Severity level.")
    ] = None,
    enabled: Annotated[
        bool | None, typer.Option("--enable/--disable", help="Enable or disable rule.")
    ] = None,
    param: Annotated[
        list[str] | None,
        typer.Option("--param", "-P", help="Rule parameter as key=value (repeatable)."),
    ] = None,
    project: ProjectOpt = None,
) -> None:
    """Create or override a detection rule globally, per campaign, or per case."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        engine = _require_engine(kernel)
        scope, scope_id, camp_id, case_id = await _resolve_scope(kernel, campaign, case)
        existing = {
            r.rule_key: r
            for r in await engine.effective_rules(campaign_id=camp_id, case_id=case_id)
        }.get(rule_key)

        resolved_kind = kind
        if resolved_kind is None:
            if existing is not None:
                resolved_kind = existing.rule_kind
            elif rule_key.startswith("monitor:"):
                resolved_kind = RuleKind.MONITOR_RANGE
            else:
                try:
                    resolved_kind = RuleKind(rule_key)
                except ValueError as exc:
                    _console.print("[red]--kind is required for custom rule keys[/]")
                    raise typer.Exit(2) from exc

        parsed_params: dict[str, Any] = dict(existing.params) if existing else {}
        for item in param or []:
            if "=" not in item:
                _console.print(f"[red]invalid --param {item!r}: expected key=value[/]")
                raise typer.Exit(2)
            k, v = item.split("=", 1)
            parsed_params[k.strip()] = _parse_param_value(v.strip())

        rec = await engine.set_rule(
            RuleSpec(
                scope_type=scope,
                scope_id=scope_id,
                rule_key=rule_key,
                rule_kind=resolved_kind,
                enabled=enabled
                if enabled is not None
                else (existing.enabled if existing else True),
                severity=(
                    severity
                    if severity is not None
                    else (existing.severity if existing else RuleSeverity.WARNING)
                ),
                params=parsed_params,
            )
        )
        _console.print(
            f"Saved rule {rec.rule_key} at {rec.scope_type}:{rec.scope_id} "
            f"(enabled={rec.enabled}, severity={rec.severity}, params={rec.params})"
        )

    _with_kernel(setup, body)


@rules_app.command("unset")
def rules_unset(
    rule_key: Annotated[str, typer.Argument(help="Rule key to remove from this scope.")],
    campaign: Annotated[
        str | None, typer.Option("--campaign", "-c", help="Campaign name or id.")
    ] = None,
    case: Annotated[
        str | None, typer.Option("--case", help="Case key (requires --campaign).")
    ] = None,
    project: ProjectOpt = None,
) -> None:
    """Remove a rule override at global, campaign, or case scope."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        engine = _require_engine(kernel)
        scope, scope_id, _, _ = await _resolve_scope(kernel, campaign, case)
        removed = await engine.unset_rule(scope, scope_id, rule_key)
        if removed:
            _console.print(f"Removed rule override {rule_key} from {scope}:{scope_id}.")
        else:
            _console.print(f"[yellow]no override found for {rule_key} at {scope}:{scope_id}[/]")

    _with_kernel(setup, body)


@alerts_app.command("list")
def alerts_list(
    open_only: Annotated[bool, typer.Option("--open", help="Only show open alerts.")] = False,
    run_id: Annotated[int | None, typer.Option("--run", "-r", help="Filter by run id.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Maximum rows.")] = 100,
    project: ProjectOpt = None,
) -> None:
    """List detection alerts."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        engine = _require_engine(kernel)
        alerts = await engine.list_alerts(open_only=open_only, run_id=run_id, limit=limit)
        if not alerts:
            _console.print("no alerts found")
            return
        table = Table(
            "id",
            "status",
            "severity",
            "run",
            "rule",
            "title",
            "ack",
            "raised (UTC)",
        )
        for a in alerts:
            table.add_row(
                str(a.id),
                str(a.status),
                str(a.severity),
                "" if a.run_id is None else str(a.run_id),
                a.rule_key,
                a.title,
                a.acknowledged_by or "",
                a.raised_at.strftime("%Y-%m-%d %H:%M"),
            )
        _console.print(table)

    _with_kernel(setup, body)


@alerts_app.command("ack")
def alerts_ack(
    alert_id: Annotated[int, typer.Argument(help="Alert id to acknowledge.")],
    by: Annotated[str, typer.Option("--by", help="Acknowledger name.")] = "cli",
    project: ProjectOpt = None,
) -> None:
    """Acknowledge an alert."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        engine = _require_engine(kernel)
        rec = await engine.acknowledge(alert_id, by=by)
        if rec is None:
            _console.print(f"[red]alert {alert_id} not found[/]")
            raise typer.Exit(1)
        _console.print(f"Acknowledged alert #{rec.id}: {rec.title}")

    _with_kernel(setup, body)


@notify_app.command("test")
def notify_test(
    title: Annotated[str, typer.Option("--title", help="Notification title.")] = (
        "SimManager test notification"
    ),
    body_text: Annotated[str, typer.Option("--body", help="Notification body.")] = (
        "Desktop notifications are working."
    ),
    project: ProjectOpt = None,
) -> None:
    """Send a test notification through every available notifier provider."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        router: NotificationService | None = kernel.services.get(notifier_contract.SERVICE_KEY)
        if router is None:
            _console.print("[red]notifier service unavailable[/] (see `simman plugins`)")
            raise typer.Exit(1)
        results = await router.send_test(title=title, body=body_text)
        if not results:
            _console.print("[yellow]no notifier providers registered[/]")
            return
        for prov, ok in results.items():
            mark = "[green]sent[/]" if ok else "[yellow]unavailable / failed[/]"
            _console.print(f"{prov}: {mark}")

    _with_kernel(setup, body)
