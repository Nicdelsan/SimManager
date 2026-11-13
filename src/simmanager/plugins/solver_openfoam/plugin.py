"""`solver.openfoam` plugin: discovery and incremental log parsing for OpenFOAM cases."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from typing import Any

from simmanager.contracts import solver as contract
from simmanager.contracts.solver import ParsedChunk, SolverStatusUpdate, WatchedFile
from simmanager.contracts.transport import RemoteSession
from simmanager.kernel import Context, Plugin

from . import parsers

KIND = "openfoam"
PARSER_LOG = "openfoam.log"

_DICT_MAX_BYTES = 128 * 1024
_PROBE_BYTES = 8 * 1024
_NON_SOLVER_LOGS = frozenset(
    {"log.blockMesh", "log.decomposePar", "log.setFields", "log.snappyHexMesh"}
)


def _is_abs(path: str) -> bool:
    return path.startswith("/") or (len(path) >= 3 and path[1:3] == ":/")


def _join(base: str, rel: str) -> str:
    return posixpath.normpath(f"{base.rstrip('/')}/{rel.lstrip('/')}")


def _rel_path(work_dir: str, full_path: str) -> str:
    w = posixpath.normpath(work_dir)
    p = posixpath.normpath(full_path)
    if p.startswith(w + "/"):
        return p[len(w) + 1 :]
    return posixpath.basename(p)


class OpenFoamSolver:
    """Implements `simmanager.contracts.solver.Solver` for OpenFOAM v2312."""

    kind = KIND

    async def discover(
        self,
        session: RemoteSession,
        work_dir: str,
        *,
        stdout_path: str | None = None,
        known_output_dir: str | None = None,
    ) -> tuple[SolverStatusUpdate | None, list[WatchedFile]]:
        wdir = posixpath.normpath(work_dir)
        status_kwargs: dict[str, Any] = {}
        meta: dict[str, Any] = {}
        app_name: str | None = None

        # 1. Read system/controlDict
        ctrl_path = _join(wdir, "system/controlDict")
        ctrl_stat = await session.stat(ctrl_path)
        if ctrl_stat is not None and not ctrl_stat.is_dir and ctrl_stat.size > 0:
            raw = await session.read_range(ctrl_path, 0, min(ctrl_stat.size, _DICT_MAX_BYTES))
            ctrl = parsers.parse_control_dict(raw.decode("utf-8", errors="replace"))
            app_name = ctrl.application
            if ctrl.application:
                meta["application"] = ctrl.application
            if ctrl.start_from:
                meta["startFrom"] = ctrl.start_from
            if ctrl.start_time is not None:
                status_kwargs["start_time"] = ctrl.start_time
            if ctrl.end_time is not None:
                status_kwargs["end_time"] = ctrl.end_time

        # 2. Read system/decomposeParDict (optional)
        dec_path = _join(wdir, "system/decomposeParDict")
        dec_stat = await session.stat(dec_path)
        if dec_stat is not None and not dec_stat.is_dir and dec_stat.size > 0:
            raw_dec = await session.read_range(dec_path, 0, min(dec_stat.size, _DICT_MAX_BYTES))
            nprocs = parsers.parse_decompose_par_dict(raw_dec.decode("utf-8", errors="replace"))
            if nprocs is not None:
                meta["numberOfSubdomains"] = nprocs

        # 3. Discover solver log(s)
        watched: list[WatchedFile] = []
        seen_paths: set[str] = set()
        candidates: list[str] = []
        if app_name:
            candidates.append(_join(wdir, f"log.{app_name}"))
        candidates.append(_join(wdir, "log.out"))
        if stdout_path:
            sp = stdout_path if _is_abs(stdout_path) else _join(wdir, stdout_path)
            candidates.append(sp)

        for entry in await session.listdir(wdir):
            name = posixpath.basename(entry.path)
            if (
                not entry.is_dir
                and name.startswith("log.")
                and name not in _NON_SOLVER_LOGS
                and entry.path not in candidates
            ):
                candidates.append(entry.path)

        for cand in candidates:
            st = await session.stat(cand)
            if st is None or st.is_dir:
                continue
            if st.size > 0:
                probe = await session.read_range(cand, 0, min(st.size, _PROBE_BYTES))
                text = probe.decode("utf-8", errors="replace")
                if (
                    "OpenFOAM" not in text
                    and "Create time" not in text
                    and "Exec   :" not in text
                    and not (app_name and posixpath.basename(cand) == f"log.{app_name}")
                ):
                    continue
            rel = _rel_path(wdir, cand)
            if rel not in seen_paths:
                seen_paths.add(rel)
                watched.append(WatchedFile(file_path=rel, remote_path=cand, parser_kind=PARSER_LOG))

        status = (
            SolverStatusUpdate(**status_kwargs, metadata=meta) if status_kwargs or meta else None
        )
        return status, watched

    def parse_header(
        self, file_path: str, parser_kind: str, raw: bytes
    ) -> tuple[dict[str, Any], int]:
        if parser_kind != PARSER_LOG:
            raise ValueError(f"unsupported parser_kind for {self.kind}: {parser_kind!r}")
        return parsers.parse_log_header(raw)

    def find_tail_alignment(self, parser_kind: str, chunk: bytes) -> int | None:
        if parser_kind != PARSER_LOG:
            raise ValueError(f"unsupported parser_kind for {self.kind}: {parser_kind!r}")
        return parsers.find_log_tail_alignment(chunk)

    def parse_chunk(
        self,
        file_path: str,
        parser_kind: str,
        text: str,
        *,
        header_data: Mapping[str, Any],
        parser_state: Mapping[str, Any],
        is_backfill: bool = False,
    ) -> ParsedChunk:
        if parser_kind != PARSER_LOG:
            raise ValueError(f"unsupported parser_kind for {self.kind}: {parser_kind!r}")
        return parsers.parse_log_chunk(
            file_path,
            text,
            header_data=header_data,
            parser_state=parser_state,
            is_backfill=is_backfill,
        )


class OpenFoamSolverPlugin(Plugin):
    name = "solver-openfoam"
    provides = f"{contract.SERVICE_PREFIX}{KIND}"
    contract_version = contract.CONTRACT_VERSION
    description = "OpenFOAM case discovery and incremental solver log parser (foamLog rules)."

    def apply(self, ctx: Context) -> OpenFoamSolver:
        return OpenFoamSolver()
