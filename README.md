# AIQ

AIQ is a deterministic, local-first work ledger for humans, tools, and agents.
It records requests, derives versioned tasks, and leases ready work without a
remote service.

> **Alpha:** Distribution and human-interface details may change before 1.0.
> Machine contracts carry their own explicit versions.

| Motive | Practice |
|---|---|
| Thrift | Never spend compute/flow, context/stock, or user time/stalls twice. |
| Determinism | Codified answers repeat; re-derived answers drift. |

| AIQ does | AIQ does not |
|---|---|
| Keep an append-only local journal | Call models or execute task work |
| Turn messages into atomic task changes | Sync state between machines |
| Derive readiness from dependencies | Require a server or account |
| Fence concurrent work with leases | Put runtime state in Git |

## Install

AIQ supports Python 3.11–3.14 on Linux and macOS. Runtime dependencies are from
the Python standard library. Its distribution contract is installer-neutral:

| Contract | Requirement |
|---|---|
| Distribution | Install `aiq-workqueue` as a standard Python distribution |
| Command | Expose the `aiq` console entry point |
| Integrations | Retain the chosen install and its recorded host tools |

| Method | Install | Resolve the launcher |
|---|---|---|
| `pipx` | `pipx install 'aiq-workqueue @ git+https://github.com/spincyc/aiq.git@main'` | `aiq_bin="$(pipx environment --value PIPX_BIN_DIR)/aiq"` |
| Standard venv | `python3 -m venv ./aiq-venv`<br>`./aiq-venv/bin/python -m pip install 'aiq-workqueue @ git+https://github.com/spincyc/aiq.git@main'` | `aiq_bin="$(pwd)/aiq-venv/bin/aiq"` |

Direct GitHub installs require Git and network access.

```sh
"$aiq_bin" --version
```

The absolute launcher needs no `PATH` change. To make the shorter `aiq`
command available, preview and then optionally apply pipx's shell change:

| Action | Command |
|---|---|
| Preview only | `pipx ensurepath --dry-run` |
| Apply after review | `pipx ensurepath` |

Restart the shell after applying it. AIQ itself never edits shell startup files
or assumes a particular pipx application directory. Examples below use `aiq`;
use `"$aiq_bin"` instead when it is not on `PATH`.

### Host package bootstrap

Clone the repository only when developing AIQ or using its host package
bootstrap:

```sh
git clone https://github.com/spincyc/aiq.git
```

```sh
make -C ./aiq install-packages
```

| Host | `AIQ_PLATFORM` | Status | Package manager |
|---|---|---|---|
| Arch Linux | `arch` | Supported | `pacman` through `sudo` |
| macOS | `macos` | Best effort; not verified here | Existing Homebrew installation |
| Other | Detected OS ID | No bundled fragment | Target exits without installing |

```sh
make -C ./aiq AIQ_PLATFORM=arch install-packages
```

Package fragments: [Arch](make/platforms/arch.mk) ·
[macOS](make/platforms/macos.mk)

Unsupported platforms disable only `install-packages`; neutral targets such as
`make verify` remain usable.

The `main` ref is the development channel. Refresh it explicitly:

```sh
pipx install --force \
  'aiq-workqueue @ git+https://github.com/spincyc/aiq.git@main'
"$(pipx environment --value PIPX_BIN_DIR)/aiq" --version

./aiq-venv/bin/python -m pip install --force-reinstall \
  'aiq-workqueue @ git+https://github.com/spincyc/aiq.git@main'
./aiq-venv/bin/aiq --version
```

AIQ never updates itself. Upgrading pipx or a host package manager does not
implicitly update pipx-managed applications. `pipx upgrade-all` updates every
unpinned pipx application when explicitly invoked; normal AIQ releases will
bump the package version so `pipx upgrade aiq-workqueue` can update AIQ alone.

After the installer refreshes AIQ, run `aiq reconcile --user` to report
whether AIQ-owned integration hooks still match the current installation and
whether the selected journal state validates; `aiq reconcile --user --apply`
re-binds only AIQ-owned hooks and migrates supported journal storage. It
never modifies pipx, venv, Homebrew, or distro-owned package environments.

## Quickstart

Run this inside the Git repository whose work you want to track. The example
captures one message, applies one atomic task creation, and verifies the
journal.

<!-- aiq-doc-test: quickstart -->
```sh
aiq journal init --scope repo

message_id=$(
  aiq ingest --scope repo --message "Create the first reusable task" --json |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["message_id"])'
)
claim_id=$(
  aiq inbox claim "$message_id" --scope repo --owner docs-example --json |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["claim"]["claim_id"])'
)

printf '%s\n' \
  '{"v":1,"expect":{},"effects":[["create","$work",{"title":"First task"}]]}' |
  aiq inbox apply "$message_id" --scope repo \
    --claim "$claim_id" --effects - --json

aiq queue peek --scope repo
aiq journal check --scope repo
```

`queue peek` is read-only. `queue next --owner OWNER` leases work and changes
state.

## Find the right operation

The CLI is the authoritative command reference. Capabilities let an agent load
one contract instead of carrying every tool description in context.

| Need | Command |
|---|---|
| List compact operations | `aiq capability list` |
| Load one operation contract | `aiq capability show inbox.apply` |
| See command flags | `aiq COMMAND --help` |
| Locate the active journal | `aiq journal path --json` |
| Capture a message without duplicates | `aiq ingest --if-new --message TEXT` |
| Inspect pending messages | `aiq inbox list` |
| Summarize bounded work state | `aiq status` |
| Preview ready work | `aiq queue peek` |
| Lease ready work | `aiq queue next --owner OWNER` |
| Verify storage and history | `aiq journal check` |
| Check local health read-only | `aiq doctor` |
| Explain task eligibility | `aiq task explain TASK-1` |
| Review recorded task history | `aiq task history TASK-1` |
| List unreleased leases | `aiq claim list` |
| File an AIQ defect report | `aiq report --summary TEXT --detail -` |
| Reconcile after an AIQ upgrade | `aiq reconcile --user` |

Normal inbox output omits raw message content. Claim a message to interpret it,
or use `--include-content` deliberately.

## Scope and state

| Scope | Selection | Storage |
|---|---|---|
| `repo` | Explicit, or `auto` inside Git | Git common directory: `aiq/journal.sqlite3` |
| `user` | Explicit, or `auto` outside Git | `${XDG_STATE_HOME:-$HOME/.local/state}/aiq/journal.sqlite3` |

Repository scope is shared by linked worktrees, but not by independent clones.
Use `aiq journal path` before capture when scope is uncertain. Journals,
leases, snapshots, and exports are private runtime state and must not be
committed.

## Integrations

| Integration | Alpha status |
|---|---|
| [Generic input](docs/integrations/generic.md) | Message, stdin, and canonical event JSON ingestion |
| [Claude Code](docs/integrations/claude.md) | Reversible user-level prompt hook |
| [Codex](docs/integrations/codex.md) | Reversible user-level prompt hook |
| [Guidance](docs/integrations/guidance.md) | Reversible AIQ-owned block in an explicit guidance file |
| Manual guidance | The terse canonical bootstrap is [`AGENTS.md`](AGENTS.md) |

AIQ never replaces an entire agent configuration. Integration installers use
preview, minimal mutation, ownership records, drift checks, and targeted
uninstall. The Claude Code and Codex integrations record their launcher
identity and absolute Python and Git executables. Their hooks run Python with
`-I`, so dotfiles, `PATH`, `PYTHONPATH`, and `PYTHONHOME` cannot redirect the
runtime.

## Documentation

| Topic | Use it for |
|---|---|
| [Concepts](docs/concepts.md) | Messages, effects, tasks, dependencies, and leases |
| [Configuration](docs/configuration.md) | Strict TOML, environment, and CLI precedence |
| [Privacy](docs/privacy.md) | Captured data, retention, permissions, and network boundary |
| [Recovery](docs/recovery.md) | Integrity checks, snapshots, leases, and current limits |
| [Integrations](docs/integrations/README.md) | Adapter boundary and safe lifecycle |
| [Contracts](docs/contracts/README.md) | Public surfaces and alpha compatibility |

## Develop

```sh
make install-packages  # Optional supported-host toolchain bootstrap
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
make verify
make ci  # Full CI-parity checks
```

AIQ is licensed under [Apache-2.0](LICENSE).
