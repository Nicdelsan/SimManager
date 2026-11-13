# SimManager

Local monitor for CFD simulations (CONVERGE CFD, OpenFOAM) running on remote HPC clusters
(SLURM) and locally. SimManager **observes**: it discovers your jobs, follows their output
files and shows progress, state and notifications. It never builds setups, edits remote files
or submits jobs on its own.

> Status: **M0 — kernel and storage**. SSH, scheduler discovery, parsers and the web UI
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
[docs/kb/](docs/kb/README.md). Enable the repository hooks once per clone:

```powershell
git config core.hooksPath .githooks
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
