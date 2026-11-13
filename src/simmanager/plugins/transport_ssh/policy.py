"""Read-only command policy (ADR 0004).

`run_readonly` accepts an argv whose executable is allowlisted here, with options that cannot
write files or run other programs. The argv is quoted for the remote POSIX shell, so no
shell syntax (pipes, redirections, `$VAR`) ever reaches the cluster from the monitor.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from simmanager.contracts.transport import CommandNotAllowedError

# executable -> allowed sub-commands (None: any arguments, subject to FORBIDDEN_ARGS)
READONLY_COMMANDS: dict[str, frozenset[str] | None] = {
    "squeue": None,
    "sacct": None,
    "sinfo": None,
    "scontrol": frozenset({"show"}),
    "ls": None,
    "stat": None,
    "find": None,
    "head": None,
    "tail": None,
    "cat": None,
    "wc": None,
    "date": None,
    "id": None,
    "hostname": None,
    "echo": None,
    "git": frozenset({"rev-parse", "status", "log", "diff", "show"}),
    # M4: list the user's tmux sessions to offer "re-attach". No global options (-f/-S/-L
    # come before the sub-command and are rejected by the sub-command check).
    "tmux": frozenset({"ls", "list-sessions"}),
}

# Options that would write files, run programs or change state.
FORBIDDEN_ARGS: dict[str, tuple[str, ...]] = {
    "find": (
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    ),
    "date": ("-s", "--set"),
    "hostname": ("-F", "--file", "-b", "--boot"),
    "git": ("-c", "--output", "--exec-path", "--ext-diff", "--textconv", "--upload-pack"),
    "tail": ("-f", "-F", "--follow"),
}


def check_readonly(argv: Sequence[str]) -> None:
    """Raise CommandNotAllowedError unless `argv` is an allowlisted read-only command."""
    if not argv:
        raise CommandNotAllowedError("empty command")
    for arg in argv:
        if any(c in arg for c in "\0\n\r"):
            raise CommandNotAllowedError(f"control character in argument {arg!r}")
    exe, args = argv[0], list(argv[1:])
    if exe not in READONLY_COMMANDS:
        raise CommandNotAllowedError(f"{exe!r} is not an allowlisted read-only command")
    if exe == "tmux" and any("#(" in arg for arg in args):
        # `#(cmd)` in a tmux format (-F, -f filter) runs a shell command.
        raise CommandNotAllowedError("tmux: '#(...)' formats run commands")

    for arg in args:
        for bad in FORBIDDEN_ARGS.get(exe, ()):
            if arg == bad or (bad.startswith("--") and arg.startswith(bad + "=")):
                raise CommandNotAllowedError(f"{exe}: option {arg!r} is not read-only")
            if exe == "git" and arg.startswith(bad) and bad.startswith("--"):
                raise CommandNotAllowedError(f"git: option {arg!r} is not read-only")

    subcommands = READONLY_COMMANDS[exe]
    if subcommands is not None:
        if exe == "git" and len(args) >= 2 and args[0] == "-C":
            args = args[2:]  # `git -C <dir> <sub>` is the only global option allowed
        if not args or args[0] not in subcommands:
            allowed = ", ".join(sorted(subcommands))
            raise CommandNotAllowedError(f"{exe}: only these sub-commands are allowed: {allowed}")


def to_command_line(argv: Sequence[str]) -> str:
    """Quote argv for the remote POSIX shell."""
    return shlex.join(argv)
