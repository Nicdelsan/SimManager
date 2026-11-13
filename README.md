# SimManager

Local monitor for CFD simulations (CONVERGE CFD, OpenFOAM) running on remote HPC clusters
(SLURM) and locally. SimManager **observes**: it discovers your jobs, follows their output
files and shows progress, state and notifications. It never builds setups, edits remote files
or submits jobs on its own.

> Status: **M1 — SSH and SLURM job discovery**. Solver parsers, plots and the web UI
> arrive in the next milestones (see `docs/architecture.md`).

## Install

Requires [uv](https://docs.astral.sh/uv/) and Git. Python is fetched by uv if needed.

```powershell
git clone https://github.com/Nicdelsan/SimManager.git
cd SimManager
uv sync
uv run simman --help
```

## Use

```powershell
uv run simman plugins               # start the kernel once, show the state of every plugin
uv run simman config show --origin  # composed configuration and the layer each value comes from
uv run simman config paths          # where config, database and logs live
uv run simman serve                 # run until Ctrl+C (web UI from M3)
uv run simman events -n 20          # last entries of the durable event log
```

### Clusters

Profiles live in `clusters.yaml` next to the user config (never in the repository).
Anything already in `~/.ssh/config` (HostName, User, IdentityFile, ProxyJump) is reused.

```powershell
uv run simman cluster add uni-a --ssh-host uni-a --timezone Europe/Rome
uv run simman cluster add hpc --host login.hpc.example.org --user me `
    --key C:\path\to\my_cert_key `
    --renew-command "C:\path\to\renew_certificate.bat"
uv run simman cluster list
uv run simman cluster test uni-a    # connects and checks the SLURM commands
uv run simman monitor --once        # poll every cluster once
uv run simman jobs list --all       # what the monitor recorded
uv run simman serve                 # keep polling (60 s running / 180 s queued / 300 s idle)
```

SimManager only runs read-only commands (`squeue`, `sacct`, `scontrol show`, `ls`, ...).
Host keys must already be in `known_hosts` (connect once with `ssh`). When a login
needs a human (password, OTP, expired SSH certificate) it asks on the terminal and never
retries a rejected login on its own; passwords are never stored. For short-lived SSH
certificates (`<key>-cert.pub`) it checks the expiry first and offers to run
`cert_renew_command`.

### Configuration

Layers, later ones win:

1. built-in defaults (including each plugin's defaults);
2. user file: `%APPDATA%\SimManager\config.yaml` (Windows) or `~/.config/simmanager/config.yaml`;
3. project file: `simmanager.yaml` in the current directory, or `--project <dir|file>`.

```yaml
log_level: INFO
plugins:
  storage-sqlite:
    path: null            # default: <data dir>/simmanager.db
  example-clock:          # demo plugins, disabled by default
    enabled: true
    interval_s: 2
  example-ticker:
    enabled: true
```

Data (database, logs) lives in `%LOCALAPPDATA%\SimManager` on Windows and
`~/.local/share/simmanager` on Linux. `SIMMANAGER_CONFIG_DIR` and `SIMMANAGER_DATA_DIR`
override both.

## Develop

Humans and coding agents: start from [AGENTS.md](AGENTS.md) and the knowledge base in
[docs/kb/](docs/kb/README.md). Once per clone (automatic in Claude Code sessions):

```powershell
uv run python tools/dev_setup.py   # enables git hooks, builds the local privacy denylist
```

```powershell
uv run pytest           # tests
uv run ruff check src tests && uv run ruff format src tests
uv run mypy             # strict on simmanager.kernel and simmanager.contracts
```

Real cluster samples stay in `samples/`, which is **git-ignored**: they contain usernames,
paths and accounts. Tests only use anonymized, truncated extracts in `tests/fixtures/`.

## Writing a plugin

```python
from simmanager.kernel import Context, Plugin

class MyPlugin(Plugin):
    name = "my-plugin"
    inject = {"storage": 1}           # required services and contract major version

    def apply(self, ctx: Context) -> None:
        storage = ctx.services.require("storage")
        ctx.task(self.loop(storage), name="loop")   # cancelled automatically on unload
```

External plugins are pip packages exposing an entry point:

```toml
[project.entry-points."simmanager.plugins"]
my-plugin = "my_package.plugin:MyPlugin"
```
