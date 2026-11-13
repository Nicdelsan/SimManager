"""Pure textual and semantic diffing for CONVERGE `.in` (YAML) and OpenFOAM dicts."""

from __future__ import annotations

import difflib
import posixpath
import re
from typing import Any

import yaml

from simmanager.contracts.snapshot import (
    ContentStatus,
    DiffChangeKind,
    DiffSummary,
    FileDiff,
    SemanticChange,
    SnapshotBlob,
    SnapshotDiff,
    SnapshotFileRecord,
    SnapshotRecord,
)

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_FOAM_KV_RE = re.compile(r"^[ \t]*([A-Za-z0-9_.-]+)[ \t]+([^;{}\n]+?)[ \t]*;", re.MULTILINE)
_SUBDICT_START_RE = re.compile(r"^[ \t]*([A-Za-z0-9_.-]+)[ \t]*\n?[ \t]*\{", re.MULTILINE)


def _format_scalar(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return repr(val)
    if isinstance(val, str):
        return val
    return str(val)


def _flatten_value(prefix: str, val: Any, out: dict[str, str | None]) -> None:
    if isinstance(val, dict):
        if not val:
            out[prefix] = "{}"
            return
        for k, v in val.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            _flatten_value(key, v, out)
        return
    if isinstance(val, list):
        if len(val) <= 8 and all(not isinstance(item, (dict, list)) for item in val):
            out[prefix] = "[" + ", ".join(str(x) for x in val) + "]"
        else:
            for idx, item in enumerate(val):
                _flatten_value(f"{prefix}[{idx}]", item, out)
        return
    out[prefix] = _format_scalar(val)


def flatten_yaml_documents(text: str) -> list[dict[str, str | None]]:
    """Parse multi-document YAML (e.g. CONVERGE `.in`) into flattened dotted key maps."""
    docs: list[dict[str, str | None]] = []
    try:
        parsed = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return []
    for doc in parsed:
        flat: dict[str, str | None] = {}
        if isinstance(doc, dict):
            _flatten_value("", doc, flat)
        elif doc is not None:
            flat["<root>"] = _format_scalar(doc)
        docs.append(flat)
    return docs


def diff_yaml_semantic(
    base_text: str | None, target_text: str | None
) -> tuple[SemanticChange, ...]:
    """Compute semantic key differences across YAML documents."""
    base_docs = flatten_yaml_documents(base_text) if base_text is not None else []
    target_docs = flatten_yaml_documents(target_text) if target_text is not None else []
    if not base_docs and not target_docs:
        return ()

    n_docs = max(len(base_docs), len(target_docs))
    multi = n_docs > 1
    changes: list[SemanticChange] = []
    for idx in range(n_docs):
        bmap = base_docs[idx] if idx < len(base_docs) else {}
        tmap = target_docs[idx] if idx < len(target_docs) else {}
        for key in sorted(set(bmap) | set(tmap)):
            old_v = bmap.get(key)
            new_v = tmap.get(key)
            if old_v != new_v:
                changes.append(
                    SemanticChange(
                        path=key,
                        old_value=old_v,
                        new_value=new_v,
                        doc_index=idx if multi else None,
                    )
                )
    return tuple(changes)


def _strip_foam_comments(text: str) -> str:
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def parse_openfoam_dict_keys(text: str) -> dict[str, str]:
    """Extract top-level and 1-level nested `key value;` entries from an OpenFOAM dictionary."""
    cleaned = _strip_foam_comments(text)
    result: dict[str, str] = {}

    # Top-level text outside braces
    top_chars: list[str] = []
    depth = 0
    for ch in cleaned:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            top_chars.append(ch)
    top_text = "".join(top_chars)
    for m in _FOAM_KV_RE.finditer(top_text):
        result[m.group(1)] = " ".join(m.group(2).split())

    # 1-level sub-dictionaries (excluding FoamFile header)
    i = 0
    n = len(cleaned)
    while i < n:
        match = _SUBDICT_START_RE.search(cleaned, i)
        if match is None:
            break
        block_name = match.group(1)
        brace_start = match.end() - 1
        depth = 1
        j = brace_start + 1
        while j < n and depth > 0:
            if cleaned[j] == "{":
                depth += 1
            elif cleaned[j] == "}":
                depth -= 1
            j += 1
        if depth == 0 and block_name != "FoamFile":
            inner = cleaned[brace_start + 1 : j - 1]
            # Keep only immediate keys inside this block (depth 0 relative to inner)
            inner_top: list[str] = []
            sub_d = 0
            for ch in inner:
                if ch == "{":
                    sub_d += 1
                elif ch == "}":
                    sub_d = max(0, sub_d - 1)
                elif sub_d == 0:
                    inner_top.append(ch)
            for km in _FOAM_KV_RE.finditer("".join(inner_top)):
                result[f"{block_name}.{km.group(1)}"] = " ".join(km.group(2).split())
        i = j
    return result


def diff_openfoam_semantic(
    base_text: str | None, target_text: str | None
) -> tuple[SemanticChange, ...]:
    bmap = parse_openfoam_dict_keys(base_text) if base_text is not None else {}
    tmap = parse_openfoam_dict_keys(target_text) if target_text is not None else {}
    changes: list[SemanticChange] = []
    for key in sorted(set(bmap) | set(tmap)):
        old_v = bmap.get(key)
        new_v = tmap.get(key)
        if old_v != new_v:
            changes.append(SemanticChange(path=key, old_value=old_v, new_value=new_v))
    return tuple(changes)


def is_converge_yaml_path(rel_path: str) -> bool:
    return posixpath.basename(rel_path).lower().endswith(".in")


def is_openfoam_dict_path(rel_path: str) -> bool:
    norm = rel_path.lstrip("/")
    return norm.startswith(("system/", "constant/", "0/", "0.orig/"))


def _semantic_changes_for(
    rel_path: str,
    solver: str | None,
    base_text: str | None,
    target_text: str | None,
) -> tuple[SemanticChange, ...]:
    if solver == "converge" or is_converge_yaml_path(rel_path):
        return diff_yaml_semantic(base_text, target_text)
    if solver == "openfoam" or is_openfoam_dict_path(rel_path):
        return diff_openfoam_semantic(base_text, target_text)
    return ()


def _decode_blob(blob: SnapshotBlob | None) -> str | None:
    if blob is None or not blob.is_text:
        return None
    return blob.data.decode(blob.encoding, errors="replace")


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "?"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _make_unified_diff(rel_path: str, base_text: str | None, target_text: str | None) -> str | None:
    if base_text is None and target_text is None:
        return None
    b_lines = (base_text or "").splitlines(keepends=True)
    t_lines = (target_text or "").splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            b_lines,
            t_lines,
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )
    return "".join(diff_lines) if diff_lines else None


def _is_oversize(entry: SnapshotFileRecord) -> bool:
    return entry.content_status in (
        ContentStatus.OVERSIZE_HASHED,
        ContentStatus.OVERSIZE_UNHASHED,
    )


def compute_snapshot_diff(
    base: SnapshotRecord | None,
    target: SnapshotRecord,
    blobs: dict[str, SnapshotBlob],
    *,
    base_reason: str,
) -> SnapshotDiff:
    """Compute a complete textual + semantic diff between two snapshots (pure)."""
    base_by_path = {f.rel_path: f for f in base.files} if base is not None else {}
    target_by_path = {f.rel_path: f for f in target.files}
    solver = target.solver or (base.solver if base is not None else None)

    diffs: list[FileDiff] = []
    for rel_path in sorted(set(base_by_path) | set(target_by_path)):
        bf = base_by_path.get(rel_path)
        tf = target_by_path.get(rel_path)

        if bf is None and tf is not None:
            t_text = _decode_blob(blobs.get(tf.sha256)) if tf.sha256 else None
            sem = (
                _semantic_changes_for(rel_path, solver, None, t_text) if t_text is not None else ()
            )
            note = (
                f"large file ({_fmt_size(tf.size_bytes)}, content not saved)"
                if _is_oversize(tf)
                else None
            )
            diffs.append(
                FileDiff(
                    rel_path=rel_path,
                    change_kind=DiffChangeKind.ADDED,
                    target_size=tf.size_bytes,
                    target_sha256=tf.sha256,
                    semantic_changes=sem,
                    unified_diff=_make_unified_diff(rel_path, None, t_text),
                    note=note,
                )
            )
            continue

        if bf is not None and tf is None:
            b_text = _decode_blob(blobs.get(bf.sha256)) if bf.sha256 else None
            sem = (
                _semantic_changes_for(rel_path, solver, b_text, None) if b_text is not None else ()
            )
            note = (
                f"large file ({_fmt_size(bf.size_bytes)}, content not saved)"
                if _is_oversize(bf)
                else None
            )
            diffs.append(
                FileDiff(
                    rel_path=rel_path,
                    change_kind=DiffChangeKind.REMOVED,
                    base_size=bf.size_bytes,
                    base_sha256=bf.sha256,
                    semantic_changes=sem,
                    unified_diff=_make_unified_diff(rel_path, b_text, None),
                    note=note,
                )
            )
            continue

        assert bf is not None and tf is not None
        if (
            bf.content_status is ContentStatus.UNREADABLE
            or tf.content_status is ContentStatus.UNREADABLE
        ):
            diffs.append(
                FileDiff(
                    rel_path=rel_path,
                    change_kind=DiffChangeKind.UNREADABLE,
                    base_size=bf.size_bytes,
                    target_size=tf.size_bytes,
                    note="file unreadable during capture",
                )
            )
            continue

        if _is_oversize(bf) or _is_oversize(tf):
            if bf.sha256 and tf.sha256:
                changed = bf.sha256 != tf.sha256
                basis = "sha256"
            else:
                changed = bf.size_bytes != tf.size_bytes or abs(bf.mtime - tf.mtime) >= 1e-6
                basis = "size/mtime"
            kind = DiffChangeKind.OVERSIZE_CHANGED if changed else DiffChangeKind.OVERSIZE_UNCHANGED
            note = (
                f"oversize file ({_fmt_size(bf.size_bytes)} -> {_fmt_size(tf.size_bytes)}, "
                f"compared by {basis}, content not saved)"
            )
            diffs.append(
                FileDiff(
                    rel_path=rel_path,
                    change_kind=kind,
                    base_size=bf.size_bytes,
                    target_size=tf.size_bytes,
                    base_sha256=bf.sha256,
                    target_sha256=tf.sha256,
                    note=note,
                )
            )
            continue

        if bf.sha256 == tf.sha256 and bf.size_bytes == tf.size_bytes:
            continue

        b_text = _decode_blob(blobs.get(bf.sha256)) if bf.sha256 else None
        t_text = _decode_blob(blobs.get(tf.sha256)) if tf.sha256 else None
        sem = (
            _semantic_changes_for(rel_path, solver, b_text, t_text)
            if (b_text is not None or t_text is not None)
            else ()
        )
        diffs.append(
            FileDiff(
                rel_path=rel_path,
                change_kind=DiffChangeKind.MODIFIED,
                base_size=bf.size_bytes,
                target_size=tf.size_bytes,
                base_sha256=bf.sha256,
                target_sha256=tf.sha256,
                semantic_changes=sem,
                unified_diff=_make_unified_diff(rel_path, b_text, t_text),
            )
        )

    return SnapshotDiff(
        base_snapshot=base,
        target_snapshot=target,
        base_reason=base_reason,
        files=tuple(diffs),
        target_late=target.late,
        target_late_files=target.late_files,
        base_late=base.late if base is not None else False,
    )


def summarize_diff(diff: SnapshotDiff) -> DiffSummary:
    added = sum(1 for f in diff.files if f.change_kind is DiffChangeKind.ADDED)
    removed = sum(1 for f in diff.files if f.change_kind is DiffChangeKind.REMOVED)
    modified = sum(1 for f in diff.files if f.change_kind is DiffChangeKind.MODIFIED)
    oversize_changed = sum(
        1 for f in diff.files if f.change_kind is DiffChangeKind.OVERSIZE_CHANGED
    )
    semantic_changes = sum(len(f.semantic_changes) for f in diff.files)
    return DiffSummary(
        base_snapshot_id=diff.base_snapshot.id if diff.base_snapshot else None,
        base_run_id=diff.base_snapshot.run_id if diff.base_snapshot else None,
        base_reason=diff.base_reason,
        added=added,
        removed=removed,
        modified=modified,
        oversize_changed=oversize_changed,
        semantic_changes=semantic_changes,
    )
