"""`simman campaign` subcommands for Excel campaign import, inspection, matching, and export."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from simmanager import paths
from simmanager.app import AppSetup, prepare, start_kernel
from simmanager.contracts import campaign as campaign_contract
from simmanager.contracts.campaign import (
    CampaignService,
    CampaignStore,
    ExcelPreview,
    ExportOptions,
    ImportOptions,
)
from simmanager.kernel import ConfigError, Kernel
from simmanager.logging_setup import setup_logging

campaign_app = typer.Typer(
    no_args_is_help=True,
    help="Manage Excel simulation campaigns, run associations, and exports.",
)

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


def _require_services(kernel: Kernel) -> tuple[CampaignStore, CampaignService]:
    store: CampaignStore | None = kernel.services.get(campaign_contract.SERVICE_KEY)
    mgr: CampaignService | None = kernel.services.get(campaign_contract.MANAGER_SERVICE_KEY)
    if store is None or mgr is None:
        _console.print("[red]campaign services unavailable[/] (see `simman plugins`)")
        raise typer.Exit(1)
    return store, mgr


def _print_preview(preview: ExcelPreview) -> None:
    pt = Table("detected field", "value", title=f"Workbook preview: {preview.path.name}")
    pt.add_row("sheets", ", ".join(preview.sheets))
    pt.add_row("selected_sheet", preview.selected_sheet)
    pt.add_row("header_row", str(preview.header_row))
    pt.add_row("case_column", preview.detected_case_column or "(not detected)")
    pt.add_row("work_dir_column", preview.detected_work_dir_column or "(none)")
    pt.add_row("manual_state_column", preview.detected_manual_state_column or "(none)")
    pt.add_row("columns", ", ".join(preview.columns))
    pt.add_row("data_rows", str(preview.total_data_rows))
    _console.print(pt)


@campaign_app.command("import")
def campaign_import(
    xlsx_path: Annotated[Path, typer.Argument(help="Path to the .xlsx campaign workbook.")],
    name: Annotated[
        str | None, typer.Option("--name", "-n", help="Campaign name (default: file stem).")
    ] = None,
    project_name: Annotated[
        str, typer.Option("--project-name", help="Project name grouping campaigns.")
    ] = "default",
    sheet: Annotated[str | None, typer.Option("--sheet", "-s", help="Worksheet name.")] = None,
    header_row: Annotated[
        int | None, typer.Option("--header-row", help="1-based header row index.")
    ] = None,
    case_col: Annotated[
        str | None, typer.Option("--case-col", help="Column holding case identifiers.")
    ] = None,
    workdir_col: Annotated[
        str | None, typer.Option("--workdir-col", help="Column holding cluster work_dir paths.")
    ] = None,
    state_col: Annotated[
        str | None, typer.Option("--state-col", help="Column holding manual state text.")
    ] = None,
    work_root: Annotated[
        str | None,
        typer.Option(
            "--work-root",
            help="Cluster directory root scoping name/regex auto-matching.",
        ),
    ] = None,
    naming_regex: Annotated[
        str | None,
        typer.Option(
            "--naming-regex", help="Regex with (?P<case>...) and optional (?P<parent>...)."
        ),
    ] = None,
    stop_on_blank: Annotated[
        bool,
        typer.Option(
            "--stop-on-blank/--no-stop-on-blank",
            help="Stop reading at the first row with an empty case cell.",
        ),
    ] = True,
    apply_renames: Annotated[
        bool,
        typer.Option(
            "--apply-renames",
            help="Apply detected case_key renames (same work_dir) preserving linked runs.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show import diff without writing to storage."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", "-y", help="Accept auto-detected layout without interactive prompts."
        ),
    ] = False,
    project: ProjectOpt = None,
) -> None:
    """Import or idempotently reimport an Excel campaign workbook."""
    if not xlsx_path.is_file():
        _console.print(f"[red]workbook not found:[/] {xlsx_path}")
        raise typer.Exit(2)

    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        _, mgr = _require_services(kernel)
        preview = await mgr.preview_xlsx(xlsx_path, sheet_name=sheet, header_row=header_row)
        _print_preview(preview)

        chosen_sheet = sheet or preview.selected_sheet
        chosen_header = header_row if header_row is not None else preview.header_row
        chosen_case = case_col or preview.detected_case_column
        chosen_wd = workdir_col if workdir_col is not None else preview.detected_work_dir_column
        chosen_state = state_col if state_col is not None else preview.detected_manual_state_column

        all_explicit = sheet is not None and header_row is not None and case_col is not None
        if not yes and not all_explicit:
            chosen_sheet = typer.prompt("Sheet name", default=chosen_sheet)
            if chosen_sheet != preview.selected_sheet or header_row is None:
                preview2 = await mgr.preview_xlsx(
                    xlsx_path, sheet_name=chosen_sheet, header_row=header_row
                )
                chosen_header = int(
                    typer.prompt("Header row (1-based)", default=str(preview2.header_row))
                )
                preview2 = await mgr.preview_xlsx(
                    xlsx_path, sheet_name=chosen_sheet, header_row=chosen_header
                )
                chosen_case = case_col or preview2.detected_case_column
                chosen_wd = (
                    workdir_col if workdir_col is not None else preview2.detected_work_dir_column
                )
                chosen_state = (
                    state_col if state_col is not None else preview2.detected_manual_state_column
                )
            chosen_case = typer.prompt("Case column", default=chosen_case or "# Case")
            wd_ans = typer.prompt(
                "Work-dir column (blank for none)", default=chosen_wd or ""
            ).strip()
            chosen_wd = wd_ans or None
            st_ans = typer.prompt(
                "Manual state column (blank for none)", default=chosen_state or ""
            ).strip()
            chosen_state = st_ans or None
            if not typer.confirm("Proceed with import?", default=True):
                raise typer.Exit(1)

        if not chosen_case:
            _console.print("[red]case column could not be determined[/]; pass --case-col")
            raise typer.Exit(2)

        opts = ImportOptions(
            project_name=project_name,
            campaign_name=name,
            sheet_name=chosen_sheet,
            header_row=chosen_header,
            case_column=chosen_case,
            work_dir_column=chosen_wd,
            manual_state_column=chosen_state,
            work_root=work_root,
            naming_regex=naming_regex
            if naming_regex is not None
            else campaign_contract.DEFAULT_NAMING_REGEX,
            stop_on_blank_case=stop_on_blank,
            apply_renames=apply_renames,
            dry_run=dry_run,
        )
        try:
            rep = await mgr.import_xlsx(xlsx_path, opts)
        except ValueError as exc:
            _console.print(f"[red]import failed:[/] {exc}")
            raise typer.Exit(2) from exc

        tag = " (dry-run)" if rep.dry_run else ""
        rt = Table("metric", "value", title=f"Campaign {rep.campaign_name}{tag}")
        rt.add_row("project", rep.project_name)
        rt.add_row("added", f"{len(rep.added)} ({', '.join(rep.added[:10])})" if rep.added else "0")
        rt.add_row(
            "updated", f"{len(rep.updated)} ({', '.join(rep.updated[:10])})" if rep.updated else "0"
        )
        rt.add_row("unchanged", str(len(rep.unchanged)))
        rt.add_row(
            "missing", f"{len(rep.missing)} ({', '.join(rep.missing[:10])})" if rep.missing else "0"
        )
        if rep.renamed:
            pairs = ", ".join(f"{o}->{n}" for o, n in rep.renamed)
            rt.add_row("renamed", f"{len(rep.renamed)} ({pairs})")
        rt.add_row("matched_runs", str(rep.matched_runs))
        rt.add_row("ambiguous_runs", str(rep.ambiguous_runs))
        _console.print(rt)

    _with_kernel(setup, body)


@campaign_app.command("list")
def campaign_list(
    project_name: Annotated[
        str | None, typer.Option("--project-name", help="Filter by project name.")
    ] = None,
    project: ProjectOpt = None,
) -> None:
    """List imported campaigns and their case counts."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        store, mgr = _require_services(kernel)
        projs = {p.id: p.name for p in await store.projects()}
        camps = await mgr.list_campaigns(project_name=project_name)
        if not camps:
            _console.print("no campaigns imported yet: run `simman campaign import <file.xlsx>`")
            return
        table = Table("id", "project", "name", "cases", "states", "work_root", "source")
        for c in camps:
            cases = await store.cases(c.id, include_inactive=False)
            counts = Counter(str(x.computed_state) for x in cases)
            summary = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
            table.add_row(
                str(c.id),
                projs.get(c.project_id, str(c.project_id)),
                c.name,
                str(len(cases)),
                summary,
                c.work_root or "",
                c.source_path or "",
            )
        _console.print(table)

    _with_kernel(setup, body)


@campaign_app.command("show")
def campaign_show(
    campaign: Annotated[str, typer.Argument(help="Campaign name or id.")],
    project_name: Annotated[
        str | None, typer.Option("--project-name", help="Project name to disambiguate.")
    ] = None,
    case: Annotated[
        str | None, typer.Option("--case", "-c", help="Show detailed view for a single case key.")
    ] = None,
    all_params: Annotated[
        bool, typer.Option("--all-params", help="Include all Excel parameter columns in the table.")
    ] = False,
    include_inactive: Annotated[
        bool, typer.Option("--include-inactive", help="Include cases missing from latest import.")
    ] = False,
    project: ProjectOpt = None,
) -> None:
    """Show a campaign's cases, computed states, linked runs, or detail of one case."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        _, mgr = _require_services(kernel)
        try:
            if case is not None:
                camp, summary = await mgr.show_case(campaign, case, project_name=project_name)
                _print_case_detail(camp.name, summary)
                return
            camp, summaries = await mgr.show_campaign(
                campaign, project_name=project_name, include_inactive=include_inactive
            )
        except (KeyError, ValueError) as exc:
            _console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc

        extra_cols = (
            [
                col
                for col in camp.columns
                if col not in {camp.case_column, camp.work_dir_column, camp.manual_state_column}
            ]
            if all_params
            else []
        )
        table = Table(
            "case",
            "parent",
            "manual",
            "computed",
            "job",
            "cluster",
            "progress",
            "runs",
            "work_dir",
            *extra_cols,
            title=f"Campaign {camp.name} (work_root={camp.work_root or '-'})",
        )
        for s in summaries:
            latest = s.latest
            job_id = latest.job.info.job_id if latest and latest.job else ""
            cluster = (
                latest.run.cluster_id
                if latest and latest.run.cluster_id
                else (latest.job.cluster_id if latest and latest.job else "")
            )
            progress = (
                f"{latest.solver_state.progress * 100:.1f}%"
                if latest and latest.solver_state and latest.solver_state.progress is not None
                else ""
            )
            key_label = (
                s.case.case_key if s.case.active else f"{s.case.case_key} [dim](inactive)[/]"
            )
            row = [
                key_label,
                s.parent_case_key
                or (f"({s.case.parent_key_raw})" if s.case.parent_key_raw else ""),
                s.case.manual_state or "",
                str(s.case.computed_state),
                job_id,
                cluster,
                progress,
                str(len(s.runs)),
                s.case.work_dir or "",
                *(s.case.params.get(col, "") for col in extra_cols),
            ]
            table.add_row(*row)
        _console.print(table)

    _with_kernel(setup, body)


def _print_case_detail(campaign_name: str, summary: campaign_contract.CaseSummary) -> None:
    c = summary.case
    meta = Table("field", "value", title=f"{campaign_name} :: Case {c.case_key}")
    meta.add_row("computed_state", str(c.computed_state))
    meta.add_row("manual_state", c.manual_state or "-")
    meta.add_row("active", str(c.active))
    meta.add_row("work_dir", c.work_dir or "-")
    meta.add_row(
        "parent",
        summary.parent_case_key
        or (f"unresolved ({c.parent_key_raw})" if c.parent_key_raw else "-"),
    )
    meta.add_row("children", ", ".join(summary.children_keys) if summary.children_keys else "-")
    for k, v in c.params.items():
        meta.add_row(f"param:{k}", v)
    _console.print(meta)

    if summary.runs:
        link_by_run = {lk.run_id: lk for lk in summary.links}
        rt = Table("run_id", "match", "cluster", "job_id", "job_state", "solver_state", "work_dir")
        for rf in summary.runs:
            lk = link_by_run.get(rf.run.id)
            rt.add_row(
                str(rf.run.id),
                f"{lk.match_kind}{'*' if lk and lk.locked else ''}" if lk else "",
                rf.run.cluster_id or "",
                rf.job.info.job_id if rf.job else "",
                str(rf.job.info.state) if rf.job else "",
                str(rf.solver_state.solver_state) if rf.solver_state else "",
                rf.run.work_dir or "",
            )
        _console.print(rt)


@campaign_app.command("match")
def campaign_match(
    campaign: Annotated[str, typer.Argument(help="Campaign name or id.")],
    case_key: Annotated[str, typer.Argument(help="Target case key.")],
    run_id: Annotated[int, typer.Argument(help="Run id to associate.")],
    project_name: Annotated[
        str | None, typer.Option("--project-name", help="Project name to disambiguate.")
    ] = None,
    project: ProjectOpt = None,
) -> None:
    """Manually associate RUN_ID with CASE_KEY (locked against auto-matching)."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        _, mgr = _require_services(kernel)
        try:
            link = await mgr.match_run(campaign, case_key, run_id, project_name=project_name)
        except (KeyError, ValueError) as exc:
            _console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        _console.print(
            f"[green]matched[/] run {link.run_id} -> case {case_key} (locked={link.locked})"
        )

    _with_kernel(setup, body)


@campaign_app.command("unmatch")
def campaign_unmatch(
    campaign: Annotated[str, typer.Argument(help="Campaign name or id.")],
    run_id: Annotated[int, typer.Argument(help="Run id to unlink.")],
    project_name: Annotated[
        str | None, typer.Option("--project-name", help="Project name to disambiguate.")
    ] = None,
    project: ProjectOpt = None,
) -> None:
    """Unlink RUN_ID from its case and exclude it from future auto-matching."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        _, mgr = _require_services(kernel)
        try:
            removed = await mgr.unmatch_run(campaign, run_id, project_name=project_name)
        except (KeyError, ValueError) as exc:
            _console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        if removed is None:
            _console.print(f"[yellow]run {run_id} was not linked in {campaign}[/]")
        else:
            _console.print(f"[green]unmatched[/] run {run_id} (excluded from auto-match)")

    _with_kernel(setup, body)


@campaign_app.command("unmatched")
def campaign_unmatched(
    campaign: Annotated[str, typer.Argument(help="Campaign name or id.")],
    project_name: Annotated[
        str | None, typer.Option("--project-name", help="Project name to disambiguate.")
    ] = None,
    project: ProjectOpt = None,
) -> None:
    """Show cases never launched and runs not matched to any case."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        _, mgr = _require_services(kernel)
        try:
            rep = await mgr.unmatched(campaign, project_name=project_name)
        except (KeyError, ValueError) as exc:
            _console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc

        ct = Table(
            "case", "manual_state", "work_dir", title=f"Unlaunched cases ({rep.campaign.name})"
        )
        for c in rep.unlaunched_cases:
            ct.add_row(c.case_key, c.manual_state or "", c.work_dir or "")
        _console.print(ct)

        rt = Table(
            "run_id",
            "cluster",
            "job_id",
            "job_name",
            "state",
            "work_dir",
            "ambiguous_candidates",
            title=f"Unmatched runs ({rep.campaign.name})",
        )
        for u in rep.unmatched_runs:
            rt.add_row(
                str(u.run.id),
                u.run.cluster_id or "",
                u.job.info.job_id if u.job else "",
                u.job.info.name if u.job else "",
                str(u.job.info.state) if u.job else u.run.origin,
                u.run.work_dir or "",
                ", ".join(u.ambiguous_case_keys),
            )
        _console.print(rt)

    _with_kernel(setup, body)


@campaign_app.command("export")
def campaign_export(
    campaign: Annotated[str, typer.Argument(help="Campaign name or id.")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output .xlsx path (never overwrites the original)."),
    ] = None,
    monitor: Annotated[
        list[str] | None,
        typer.Option(
            "--monitor",
            "-m",
            help="Monitor selector file:series[:qualifier] (repeatable).",
        ),
    ] = None,
    include_inactive: Annotated[
        bool, typer.Option("--include-inactive", help="Include inactive cases.")
    ] = False,
    project_name: Annotated[
        str | None, typer.Option("--project-name", help="Project name to disambiguate.")
    ] = None,
    project: ProjectOpt = None,
) -> None:
    """Export an enriched copy of the campaign workbook with SimManager columns."""
    setup = _prepare(project)

    async def body(kernel: Kernel) -> None:
        _, mgr = _require_services(kernel)
        opts = ExportOptions(
            output_path=output,
            monitors=tuple(monitor) if monitor is not None else None,
            include_inactive=include_inactive,
        )
        try:
            rep = await mgr.export_xlsx(campaign, opts, project_name=project_name)
        except (KeyError, ValueError) as exc:
            _console.print(f"[red]export failed:[/] {exc}")
            raise typer.Exit(2) from exc
        _console.print(
            f"[green]exported[/] {rep.cases_exported} case(s) -> {rep.output_path} "
            f"(template={rep.used_template}, added={', '.join(rep.columns_added)})"
        )

    _with_kernel(setup, body)
