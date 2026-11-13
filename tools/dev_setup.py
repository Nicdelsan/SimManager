"""Developer environment setup. Idempotent: safe to run at every agent session start.

1. Enables the versioned git hooks: `git config core.hooksPath .githooks` (local config).
2. Rebuilds the privacy denylist `.git/info/privacy-denylist` (inside .git/, never committed)
   by harvesting sensitive terms from sources that exist only on this machine:
     - the OS user name, git user.name / user.email;
     - ~/.ssh/config (host aliases, host names, users, jump hosts);
     - SimManager's clusters.yaml (hosts, users, work roots);
     - samples/ (prompt user@host, UserId=, --account=, --mail-user=, top path segments);
     - manual extras in <user config dir>/privacy-denylist.txt (one term per line).
   Terms that already appear in tracked files or in the origin URL are left out (they are
   either generic or public) and reported, so that a real leak never goes unnoticed.

Usage:  uv run python tools/dev_setup.py [--quiet] [--check]
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import re
import subprocess
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path

import yaml

from simmanager import paths

ROOT = Path(__file__).resolve().parents[1]
MANUAL_FILE_NAME = "privacy-denylist.txt"
SAMPLE_READ_BYTES = 256 * 1024
TEXT_SUFFIXES = {"", ".txt", ".sh", ".in", ".out", ".log", ".dat", ".echo", ".err", ".qtxt"}

# Generic words that must never become terms (they would block normal code and docs).
STOPWORDS = {
    "login",
    "www",
    "com",
    "org",
    "net",
    "edu",
    "local",
    "localhost",
    "cluster",
    "node",
    "nodes",
    "frontend",
    "server",
    "master",
    "head",
    "host",
    "github",
    "gitlab",
    "home",
    "usr",
    "opt",
    "share",
    "sw",
    "bin",
    "lib",
    "lib64",
    "tmp",
    "dev",
    "null",
    "proc",
    "etc",
    "var",
    "work",
    "data",
    "scratch",
    "users",
    "user",
    "root",
    "mnt",
    "srv",
    "run",
    "build",
    "src",
    "test",
    "tests",
    "shared",
    "public",
    "project",
    "projects",
    "apps",
    "software",
    "modules",
    "default",
    "debug",
    "none",
    "true",
    "false",
    "unknown",
    "intel",
    "slurm",
    "interactive",
    "normal",
    "main",
    "prod",
    "batch",
    "gpu",
    "cpu",
    "access",
    "file",
    # CFD / project vocabulary that appears in paths but is not private
    "converge",
    "openfoam",
    "simmanager",
    "large",
    "dynamic",
    "equation",
    "routine",
    "locks",
}

_PROMPT = re.compile(r"^\s*\[?([A-Za-z][\w.-]{1,31})@([A-Za-z][\w.-]{1,63})[\s:]", re.M)
_USERID = re.compile(r"\bUserId=([\w.-]+)\(")
_ACCOUNT = re.compile(r"(?:--account=|\bAccount=)([\w.-]+)")
_MAIL_USER = re.compile(r"--mail-user=([\w.%+-]+)@")
_ABS_PATH = re.compile(r"(?<![\w.~])/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
_FQDN = re.compile(r"\b([A-Za-z][\w-]*(?:\.[A-Za-z][\w-]*){2,})\b")


# ----------------------------------------------------------------------- harvesting
def split_tokens(value: str) -> set[str]:
    """Distinctive parts of a host/alias/email local part: `a-b.c_d` -> {a, b, c, d}."""
    return {t for t in re.split(r"[^A-Za-z0-9]+", value) if len(t) >= 4}


def harvest_ssh_config(text: str) -> set[str]:
    terms: set[str] = set()
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[0].startswith("#"):
            continue
        key, value = parts[0].lower(), parts[1].strip().strip('"')
        if key in {"host", "hostname", "proxyjump"}:
            for item in re.split(r"[\s,]+", value):
                item = item.split("@")[-1].split(":")[0]
                if item and not any(c in item for c in "*?!"):
                    terms.add(item)
                    terms |= split_tokens(item)
        elif key == "user":
            terms.add(value)
    return terms


def harvest_sample_text(text: str) -> set[str]:
    terms: set[str] = set()
    for user, host in _PROMPT.findall(text):
        terms.add(user)
        terms.add(host)
    terms.update(_USERID.findall(text))
    for account in _ACCOUNT.findall(text):
        terms.add(account)
        terms |= split_tokens(account)
    for local in _MAIL_USER.findall(text):
        terms.add(local)
        terms |= split_tokens(local)
    for segments in _ABS_PATH.findall(text):
        for segment in segments:
            terms.add(segment)
            terms |= split_tokens(segment)
    for host in _FQDN.findall(text):
        terms |= split_tokens(host)
    return terms


def xlsx_text(file: Path) -> str:
    """Shared strings of an .xlsx (a zip of XML): enough to find paths and names."""
    try:
        with zipfile.ZipFile(file) as zf:
            return zf.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile, OSError):
        return ""


def harvest_clusters_yaml(data: object) -> set[str]:
    terms: set[str] = set()
    clusters = data.get("clusters", {}) if isinstance(data, dict) else {}
    for profile in clusters.values() if isinstance(clusters, dict) else []:
        if not isinstance(profile, dict):
            continue
        for key in ("host", "user", "ssh_config_host", "jump_host"):
            value = profile.get(key)
            if isinstance(value, str) and value:
                terms.add(value)
                terms |= split_tokens(value)
        for root in profile.get("work_roots") or []:
            if isinstance(root, str):
                terms.update(seg for seg in root.split("/")[1:3] if seg)
    return terms


def clean(terms: Iterable[str]) -> set[str]:
    out = set()
    for term in terms:
        t = term.strip().lower()
        if len(t) < 3 or t in STOPWORDS or re.fullmatch(r"[\d.]+", t):
            continue
        out.add(t)
    return out


# --------------------------------------------------------------------------- sources
def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def collect_terms(samples_dir: Path) -> set[str]:
    raw: set[str] = {getpass.getuser()}
    for key in ("user.name", "user.email"):
        value = _git("config", key)
        if value:
            local = value.split("@")[0]
            raw.add(local)
            raw |= split_tokens(local)

    ssh_config = Path.home() / ".ssh" / "config"
    if ssh_config.is_file():
        raw |= harvest_ssh_config(ssh_config.read_text(encoding="utf-8", errors="replace"))

    clusters = paths.clusters_file()
    if clusters.is_file():
        with contextlib.suppress(yaml.YAMLError):  # a broken clusters.yaml is reported elsewhere
            raw |= harvest_clusters_yaml(yaml.safe_load(clusters.read_text(encoding="utf-8")))

    if samples_dir.is_dir():
        for file in samples_dir.rglob("*"):
            if file.is_file() and file.suffix.lower() == ".xlsx":
                raw |= harvest_sample_text(xlsx_text(file))
            elif file.is_file() and file.suffix.lower() in TEXT_SUFFIXES:
                with file.open("rb") as fh:
                    head = fh.read(SAMPLE_READ_BYTES).decode("utf-8", errors="replace")
                raw |= harvest_sample_text(head)
    return clean(raw)


def manual_terms() -> set[str]:
    file = paths.config_dir() / MANUAL_FILE_NAME
    if not file.exists():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(
            "# Extra private terms for the SimManager pre-commit privacy scan.\n"
            "# One per line, case-insensitive. This file never leaves this machine.\n",
            encoding="utf-8",
        )
        return set()
    lines = file.read_text(encoding="utf-8").splitlines()
    return {ln.strip().lower() for ln in lines if ln.strip() and not ln.startswith("#")}


def public_or_tracked(terms: set[str]) -> tuple[set[str], set[str]]:
    """Split out terms in the origin URL (public) and terms already in tracked files."""
    origin = _git("remote", "get-url", "origin").lower()
    public = {t for t in terms if t in origin}
    tracked = set()
    for term in sorted(terms - public):
        hit = subprocess.run(
            ["git", "grep", "-q", "-i", "-F", "-e", term, "--", ".", ":!uv.lock"],
            cwd=ROOT,
            capture_output=True,
        )
        if hit.returncode == 0:
            tracked.add(term)
    return public, tracked


# ----------------------------------------------------------------------------- main
def denylist_path() -> Path:
    git_path = _git("rev-parse", "--git-path", "info/privacy-denylist")
    return (ROOT / git_path) if git_path else ROOT / ".git" / "info" / "privacy-denylist"


def hooks_enabled() -> bool:
    return _git("config", "core.hooksPath") == ".githooks"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true", help="print only warnings")
    parser.add_argument("--check", action="store_true", help="only verify, change nothing")
    args = parser.parse_args(argv)

    if not _git("rev-parse", "--git-dir"):
        print("dev_setup: not a git checkout, nothing to do")
        return 0

    target = denylist_path()
    if args.check:
        ok = hooks_enabled() and target.is_file()
        if not ok:
            print("dev_setup: environment not set up; run `uv run python tools/dev_setup.py`")
        return 0 if ok else 1

    if not hooks_enabled():
        _git("config", "core.hooksPath", ".githooks")
        if not args.quiet:
            print("dev_setup: git hooks enabled (core.hooksPath=.githooks)")

    auto = collect_terms(ROOT / "samples")
    manual = manual_terms()
    public, tracked = public_or_tracked(auto)
    terms = (auto - public - tracked) | manual

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("# Generated by tools/dev_setup.py - local only, never commit.\n")
        fh.write("# Edit extra terms in " + str(paths.config_dir() / MANUAL_FILE_NAME) + "\n")
        fh.writelines(f"{t}\n" for t in sorted(terms))

    leaked = tracked & manual
    if tracked:
        print(
            "dev_setup: WARNING - terms harvested from private sources already appear in "
            f"tracked files ({len(tracked)}). Generic words are fine; real names are a leak "
            "to fix: " + ", ".join(sorted(tracked)),
            file=sys.stderr if not args.quiet else sys.stdout,
        )
    if leaked:
        print(
            "dev_setup: WARNING - manual denylist terms found in tracked files: "
            + ", ".join(sorted(leaked))
        )
    if not args.quiet:
        print(f"dev_setup: privacy denylist rebuilt with {len(terms)} terms -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
