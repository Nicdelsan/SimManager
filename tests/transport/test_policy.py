"""Read-only command policy (ADR 0004)."""

from __future__ import annotations

import pytest

from simmanager.contracts.transport import CommandNotAllowedError
from simmanager.plugins.transport_ssh.policy import check_readonly, to_command_line


@pytest.mark.parametrize(
    "argv",
    [
        ["squeue", "-h", "-u", "user1", "-o", "%i|%j"],
        ["sacct", "-X", "-n", "-P", "-j", "1,2"],
        ["scontrol", "show", "job", "123_4"],
        ["sinfo", "--version"],
        ["find", "/work/user1", "-name", "*.out", "-newer", "x"],
        ["tail", "-c", "100", "/work/user1/log"],
        ["git", "-C", "/work/user1/case", "log", "-1"],
        ["id", "-un"],
    ],
)
def test_allowed(argv: list[str]) -> None:
    check_readonly(argv)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["rm", "-rf", "/"],
        ["/usr/bin/squeue"],  # paths are not allowlisted names
        ["scancel", "1"],
        ["scontrol", "update", "JobId=1"],
        ["scontrol"],
        ["find", ".", "-delete"],
        ["find", ".", "-exec", "rm", "{}", ";"],
        ["git", "checkout", "."],
        ["git", "-c", "core.pager=sh", "log"],
        ["git", "diff", "--output=/tmp/x"],
        ["date", "-s", "2020-01-01"],
        ["tail", "-f", "log"],
        ["echo", "a\nb"],
        ["bash", "-c", "squeue"],
    ],
)
def test_rejected(argv: list[str]) -> None:
    with pytest.raises(CommandNotAllowedError):
        check_readonly(argv)


def test_quoting_neutralizes_shell_syntax() -> None:
    line = to_command_line(["ls", "a b", "$(reboot)", "x;y", "|"])
    assert line == "ls 'a b' '$(reboot)' 'x;y' '|'"
