# Codex integration

The Codex integration captures each `UserPromptSubmit` event before the model
receives it.

| Property | Alpha status |
|---|---|
| Event ingestion | Installed hook calls AIQ's Codex adapter |
| Hook configuration | `$CODEX_HOME/hooks.json`, or `~/.codex/hooks.json` |
| Hook runtime | Recorded absolute Python and Git executables |
| Python isolation | `-I -m aiq` from the invoked AIQ installation |
| Ambient environment | Works with empty or hostile `PATH`; ignores Python environment overrides |
| Lifecycle | `plan`, `install`, `check`, `uninstall`, and `print` |
| Whole-file replacement | Prohibited |
| Network or telemetry | None |

## Install

The integration resolves a durable runtime:

| Setup input | Value |
|---|---|
| AIQ command from `pipx` | `$(pipx environment --value PIPX_BIN_DIR)/aiq` |
| AIQ command from a standard venv | `/absolute/path/to/venv/bin/aiq` |
| Python runtime | Captured automatically from the invoked AIQ installation |
| Git executable | Absolute result of `command -v git` |

Resolve both executables, preview, then install:

```sh
aiq_launcher="$(pipx environment --value PIPX_BIN_DIR)/aiq"
git_executable="$(command -v git)"
"$aiq_launcher" integration plan codex --user \
  --git-executable "$git_executable"
"$aiq_launcher" integration install codex --user \
  --git-executable "$git_executable"
"$aiq_launcher" integration check codex --user \
  --git-executable "$git_executable"
```

`plan` is read-only. Install minimally appends one owned
`UserPromptSubmit` group, preserves unrelated hooks and top-level fields, and
records a private manifest and backups below
`$XDG_STATE_HOME/aiq/integrations/codex/`.

AIQ records the launcher identity, its Python runtime, and Git. The hook invokes
the recorded Python as `-I -m aiq` and passes the recorded Git path. It needs no
shell startup file and cannot be redirected through `PATH`, `PYTHONPATH`, or
`PYTHONHOME`.

Run `/hooks` in Codex and review the installed command before trusting it.
`check` reports `manual_review_required` because AIQ cannot observe or automate
that external trust decision.

## Externally managed setup

```sh
"$aiq_launcher" integration print codex --user \
  --git-executable "$git_executable"
```

`print` emits a stable `hooks.json` fragment and changes no files. Use it when
another tool owns the complete configuration.

The packaged agent bootstrap is separate:

```sh
aiq integration print agents
```

This prints the exact repository-root [`AGENTS.md`](../../AGENTS.md) content.

## Lifecycle guarantees

| Guarantee | Result |
|---|---|
| Preview | `plan` performs no mutation |
| Inspection | `print` and `check` perform no mutation |
| Minimal diff | Preserve unrelated hooks and settings |
| Repeatability | Repeated install converges |
| Ownership | Record exactly what AIQ changed |
| Drift safety | Refuse to overwrite user-modified owned values |
| Reversibility | Uninstall only unchanged AIQ-owned material |
| Failure safety | Preserve valid existing configuration |

Install is idempotent and refuses symlinks, malformed configuration, inline
TOML hooks, and conflicting entries. A changed owned hook is reported as drift
instead of being silently replaced. Review and apply an explicit repair only
after inspecting a new plan.

If the recorded Python or Git executable moves, repair both runtime paths:

```sh
new_launcher=/absolute/path/to/aiq
new_git_executable="$(command -v git)"
"$new_launcher" integration plan codex --user \
  --git-executable "$new_git_executable" --repair
"$new_launcher" integration install codex --user \
  --git-executable "$new_git_executable" --repair
"$new_launcher" integration check codex --user \
  --git-executable "$new_git_executable"
```

Invoking the new AIQ installation selects its Python automatically; the flag
selects Git. Moving or deleting either recorded runtime before repair
interrupts capture.

Uninstall:

```sh
"$aiq_launcher" integration uninstall codex --user
```

It removes only an unchanged, manifest-owned hook. It preserves unrelated later
changes, retains private exact-byte backups, never restores a complete backup,
and is safe to repeat.

Those backups may include the complete pre-existing hooks file and are retained
indefinitely. See [Privacy](../privacy.md#integration-backups).

## Verify capture

After sending one prompt:

```sh
"$aiq_launcher" inbox list
"$aiq_launcher" journal check
```

The hook is silent on success, uses source `codex`, routes by the event's
absolute `cwd`, and deduplicates a repeated session/turn event.

If capture is missing, run `integration check`, inspect `journal path` from the
target repository, and verify that Codex hooks are enabled. Inline hooks in
`config.toml` conflict with the managed `hooks.json` representation; use
`integration print` when configuration is externally owned.
