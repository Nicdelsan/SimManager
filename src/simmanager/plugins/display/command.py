"""Pure helpers of the display plugin: the remote shell command, its output, safe paths.

The script body never appears in the command line (it is sent on stdin, ADR 0008): the
command line is what `action.executed` records in the event log.
"""

from __future__ import annotations

import posixpath
import re
import secrets
import shlex
from dataclasses import dataclass

from simmanager.contracts.display import IMAGE_TYPES, DisplayError, FoundImage, image_type

EXIT_MKTEMP = 97
EXIT_CD = 98
EXIT_TIMEOUT = 124
"""GNU `timeout` exit status when the script ran too long."""
MAX_LISTED = 500
_INTERPRETER_TOKEN = re.compile(r"^[A-Za-z0-9_./+=:-]+$")


def new_marker() -> str:
    return f"__SIMMAN_DISPLAY_IMAGES_{secrets.token_hex(8)}__"


def normalize_text(text: str) -> str:
    """LF line endings (scripts pasted on Windows) and a final newline."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text if not text or text.endswith("\n") else text + "\n"


def check_interpreter(interpreter: str) -> list[str]:
    """`python3`, `/opt/x/bin/python`, `python3 -u`... Raises DisplayError otherwise."""
    tokens = interpreter.split()
    if not tokens or not all(_INTERPRETER_TOKEN.match(t) for t in tokens):
        raise DisplayError(
            "interpreter: use a program name or path plus options, e.g. `python3 -u`"
        )
    return tokens


def build_command(
    *, work_dir: str, setup: str, interpreter: str, timeout_s: int, marker: str
) -> list[str]:
    """argv of a login bash that runs the script (read from stdin) in `work_dir`, then lists
    the images written meanwhile below `work_dir` after `marker`.

    Output after the marker: `<mtime>\\t<size>\\t<path relative to work_dir>` per image.
    """
    # ASSUMPTION(A402): login nodes have bash, GNU coreutils (mktemp, touch -d, timeout)
    # and GNU find (-newer, -printf), like every Linux cluster seen so far.
    names = " -o ".join(f"-iname '*{ext}'" for ext in IMAGE_TYPES)
    python = " ".join(shlex.quote(t) for t in check_interpreter(interpreter))
    lines = [
        'tmp=$(mktemp "${TMPDIR:-/tmp}/simman_display_XXXXXX.py") || exit ' + str(EXIT_MKTEMP),
        "trap 'rm -f \"$tmp\"' EXIT",
        'cat > "$tmp"',
        # Images written in the same second as the script file still count as new.
        "touch -d '2 seconds ago' \"$tmp\" 2>/dev/null",
        normalize_text(setup).rstrip("\n"),
        f"cd -- {shlex.quote(work_dir)} || exit {EXIT_CD}",
        f'timeout -k 10 {int(timeout_s)} {python} "$tmp"',
        "rc=$?",
        f"echo {marker}",
        # processor* (decomposed OpenFOAM cases) and hidden folders are skipped: huge, no plots.
        "find . \\( -name 'processor*' -o -name '.?*' \\) -prune -o -type f"
        f" \\( {names} \\) -newer \"$tmp\" -printf '%T@\\t%s\\t%P\\n' 2>/dev/null"
        f" | head -n {MAX_LISTED}",
        'exit "$rc"',
    ]
    return ["bash", "-lc", "\n".join(line for line in lines if line) + "\n"]


@dataclass(frozen=True)
class ScriptOutput:
    stdout: str
    """What the script (and the login profile) printed, without the image listing."""
    images: list[FoundImage]
    """Newest first."""
    listed: bool
    """False when the marker never appeared (the script did not get to run)."""


def parse_output(stdout: str, marker: str) -> ScriptOutput:
    head, sep, tail = stdout.rpartition(marker + "\n")
    if not sep:
        return ScriptOutput(stdout, [], False)
    images = []
    for line in tail.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or image_type(parts[2]) is None:
            continue
        try:
            images.append(FoundImage(path=parts[2], size=int(parts[1]), mtime=float(parts[0])))
        except ValueError:
            continue
    images.sort(key=lambda i: (-i.mtime, i.path))
    return ScriptOutput(head, images, True)


def safe_relpath(path: str) -> str:
    """A path relative to the run folder that cannot leave it ("" = the folder itself)."""
    if any(c in path for c in "\0\r\n"):
        raise DisplayError("invalid path")
    norm = posixpath.normpath(path.strip().replace("\\", "/") or ".")
    if norm.startswith("/") or norm == ".." or norm.startswith("../"):
        raise DisplayError("the path must stay inside the run folder")
    return "" if norm == "." else norm


def describe_exit(exit_status: int | None, timeout_s: int) -> str:
    if exit_status == EXIT_TIMEOUT:
        return f"timed out after {timeout_s} s (display.timeout_s)"
    if exit_status == EXIT_MKTEMP:
        return "cannot create a temporary file on the cluster (mktemp)"
    if exit_status == EXIT_CD:
        return "the run folder does not exist on the cluster"
    if exit_status not in (0, None):
        return f"the script exited with status {exit_status}"
    return ""
