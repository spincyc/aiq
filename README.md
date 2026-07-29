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

## Install from source

AIQ supports Python 3.11–3.14 on Linux and macOS. Runtime dependencies are from
the Python standard library.

```sh
git clone https://github.com/spincyc/aiq.git
pipx install ./aiq
aiq --version
```

Update an existing source installation:

```sh
git -C ./aiq pull --ff-only
pipx install --force ./aiq
```

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
| Inspect pending messages | `aiq inbox list` |
| Preview ready work | `aiq queue peek` |
| Lease ready work | `aiq queue next --owner OWNER` |
| Verify storage and history | `aiq journal check` |

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
| [Codex](docs/integrations/codex.md) | Reversible user-level prompt hook |
| Manual guidance | The terse canonical bootstrap is [`AGENTS.md`](AGENTS.md) |

AIQ never replaces an entire agent configuration. Integration installers use
preview, minimal mutation, ownership records, drift checks, and targeted
uninstall.

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
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
make verify
```

AIQ is licensed under [Apache-2.0](LICENSE).
