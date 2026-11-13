"""`solver.converge` plugin: discovery and incremental parsing for CONVERGE CFD cases."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from typing import Any

from simmanager.contracts import solver as contract
from simmanager.contracts.solver import ParsedChunk, SolverStatusUpdate, WatchedFile
from simmanager.contracts.transport import RemoteSession
from simmanager.kernel import Context, Plugin

from . import parsers

KIND = "converge"
PARSER_OUT = "converge.out"
PARSER_STDOUT = "converge.stdout"

_INPUTS_MAX_BYTES = 256 * 1024
_PROBE_BYTES = 64 * 1024


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


class ConvergeSolver:
    """Implements `simmanager.contracts.solver.Solver` for CONVERGE 4.x."""

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
        watched: list[WatchedFile] = []
        seen_paths: set[str] = set()

        status_kwargs: dict[str, Any] = {}
        meta: dict[str, Any] = {}

        # 1. Read inputs.in if present
        inputs_path = _join(wdir, "inputs.in")
        inputs_stat = await session.stat(inputs_path)
        if inputs_stat is not None and not inputs_stat.is_dir and inputs_stat.size > 0:
            raw_in = await session.read_range(
                inputs_path, 0, min(inputs_stat.size, _INPUTS_MAX_BYTES)
            )
            inp = parsers.parse_inputs_in(raw_in.decode("utf-8", errors="replace"))
            if inp.start_time is not None:
                status_kwargs["start_time"] = inp.start_time
            if inp.end_time is not None:
                status_kwargs["end_time"] = inp.end_time
            if inp.crank_flag is not None:
                meta["crank_flag"] = inp.crank_flag
            if inp.restart_flag is not None:
                meta["restart_flag"] = inp.restart_flag
            if inp.restart_number is not None:
                meta["restart_number"] = inp.restart_number

        # 2. Discover stdout / logout.txt
        output_dir = known_output_dir
        stdout_candidates: list[str] = []
        logout_path = _join(wdir, "logout.txt")
        stdout_candidates.append(logout_path)
        if stdout_path:
            sp = stdout_path if _is_abs(stdout_path) else _join(wdir, stdout_path)
            if sp not in stdout_candidates:
                stdout_candidates.append(sp)

        for cand in stdout_candidates:
            st = await session.stat(cand)
            if st is None or st.is_dir:
                continue
            rel = _rel_path(wdir, cand)
            if rel not in seen_paths:
                seen_paths.add(rel)
                watched.append(
                    WatchedFile(file_path=rel, remote_path=cand, parser_kind=PARSER_STDOUT)
                )
            if output_dir is None and st.size > 0:
                probe = await session.read_range(cand, 0, min(st.size, _PROBE_BYTES))
                hdr, _ = parsers.parse_stdout_header(probe)
                if "output_dir" in hdr:
                    output_dir = hdr["output_dir"]
                if "case_dir" in hdr and "case_dir" not in status_kwargs:
                    status_kwargs["case_dir"] = hdr["case_dir"]

        # 3. Discover .out files in output_dir (or outputs_* or flat work_dir)
        out_dirs: list[str] = []
        if output_dir:
            resolved_out = output_dir if _is_abs(output_dir) else _join(wdir, output_dir)
            status_kwargs["output_dir"] = resolved_out
            st_out = await session.stat(resolved_out)
            if st_out is not None and st_out.is_dir:
                out_dirs.append(resolved_out)
            else:
                local_out = _join(wdir, posixpath.basename(resolved_out))
                st_local = await session.stat(local_out)
                if st_local is not None and st_local.is_dir:
                    out_dirs.append(local_out)
        if not out_dirs:
            for entry in await session.listdir(wdir):
                name = posixpath.basename(entry.path)
                if entry.is_dir and name.startswith("outputs_"):
                    out_dirs.append(entry.path)
            out_dirs.append(wdir)

        for odir in out_dirs:
            search_dirs = [_join(odir, "stream0"), odir]
            for sdir in search_dirs:
                st_dir = await session.stat(sdir)
                if st_dir is None or not st_dir.is_dir:
                    continue
                for entry in await session.listdir(sdir):
                    if entry.is_dir or not entry.path.endswith(".out"):
                        continue
                    # In root work_dir, verify CONVERGE header so SLURM *.out logs aren't misread.
                    if sdir == wdir:
                        head = await session.read_range(entry.path, 0, 64)
                        if not head.lstrip().startswith(b"# CONVERGE"):
                            continue
                    rel = _rel_path(wdir, entry.path)
                    if rel not in seen_paths:
                        seen_paths.add(rel)
                        watched.append(
                            WatchedFile(
                                file_path=rel, remote_path=entry.path, parser_kind=PARSER_OUT
                            )
                        )

        status = (
            SolverStatusUpdate(**status_kwargs, metadata=meta) if status_kwargs or meta else None
        )
        return status, watched

    def parse_header(
        self, file_path: str, parser_kind: str, raw: bytes
    ) -> tuple[dict[str, Any], int]:
        if parser_kind == PARSER_OUT:
            return parsers.parse_out_header(raw)
        if parser_kind == PARSER_STDOUT:
            return parsers.parse_stdout_header(raw)
        raise ValueError(f"unsupported parser_kind for {self.kind}: {parser_kind!r}")

    def find_tail_alignment(self, parser_kind: str, chunk: bytes) -> int | None:
        idx = chunk.find(b"\n")
        return None if idx < 0 else idx + 1

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
        if parser_kind == PARSER_OUT:
            return parsers.parse_out_chunk(
                file_path, text, header_data=header_data, parser_state=parser_state
            )
        if parser_kind == PARSER_STDOUT:
            return parsers.parse_stdout_chunk(
                file_path,
                text,
                header_data=header_data,
                parser_state=parser_state,
                is_backfill=is_backfill,
            )
        raise ValueError(f"unsupported parser_kind for {self.kind}: {parser_kind!r}")


class ConvergeSolverPlugin(Plugin):
    name = "solver-converge"
    provides = f"{contract.SERVICE_PREFIX}{KIND}"
    contract_version = contract.CONTRACT_VERSION
    description = "CONVERGE CFD case discovery and incremental log/.out parser."

    def apply(self, ctx: Context) -> ConvergeSolver:
        return ConvergeSolver()
