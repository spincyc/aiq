# Claude Code integration

The Claude Code integration captures each `UserPromptSubmit` event before the
model receives it and gates each `Stop` event on remaining runnable work.

| Property | Alpha status |
|---|---|
| Event ingestion | Installed hook calls AIQ's Claude Code adapter |
| Completion gate | The same command under `Stop` blocks stopping while runnable work remains |
| Hook configuration | `$CLAUDE_CONFIG_DIR/settings.json`, or `~/.claude/settings.json` |
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
"$aiq_launcher" integration plan claude --user \
  --git-executable "$git_executable"
"$aiq_launcher" integration install claude --user \
  --git-executable "$git_executable"
"$aiq_launcher" integration check claude --user \
  --git-executable "$git_executable"
```

`plan` is read-only. Install minimally appends one owned group under each of
`UserPromptSubmit` and `Stop` inside the `hooks` object of the user-level
`settings.json` — both containing the same command, managed under one
manifest and one integration id — preserves every unrelated setting and
hook, and records a private manifest and backups below
`${XDG_STATE_HOME:-$HOME/.local/state}/aiq/integrations/claude/<target-id>/`.

AIQ records the launcher identity, its Python runtime, and Git. The hook
invokes the recorded Python as `-I -m aiq` and passes the recorded Git path. It
needs no shell startup file and cannot be redirected through `PATH`,
`PYTHONPATH`, or `PYTHONHOME`.

Run `/hooks` in Claude Code and review the installed command before trusting
it. `check` reports `manual_review_required` because AIQ cannot observe or
automate that external trust decision.

## Failure behavior

The hook writes nothing to stdout: Claude Code injects `UserPromptSubmit`
stdout into model context, and capture must not alter the conversation. A
capture failure exits 1 with a single-line stderr message, never 2, so a
journal problem cannot block prompting; the normative capture exit codes and
the exit-2 rationale are defined in the
[CLI contract](../contracts/cli-v1.md#integrations).

The adapter's idempotency identity covers `session_id`, the per-prompt
`prompt_id`, the working directory, and the exact content: a byte-identical
re-delivered event deduplicates, and any difference is captured as a new
message.

Claude Code also injects machine-generated content through the same
`UserPromptSubmit` channel. Capture skips a prompt only when it is
unambiguously harness-injected: after stripping surrounding whitespace, it
starts with `<task-notification` or is one whole
`<system-reminder>…</system-reminder>` block. A skipped prompt exits 0 with a
distinct `skipped` receipt, ingests nothing, and creates no journal; a prompt
that merely mentions a marker mid-string is captured normally.

## Opt-in scope

Installed hooks never create journal storage. Repo-scope capture is opt-in
by journal presence: in a Git repository whose repo journal is not
initialized, the hook exits 0 silently, captures nothing, and creates no
storage. Run `aiq journal init --scope repo` in the repository to opt in
and `aiq journal destroy` to opt out. Outside any Git repository the event
routes to the user journal, which still auto-initializes on capture.

## Completion gate

The installed command is also registered under the `Stop` event. When Claude
Code is about to finish a turn, the hook resolves the AIQ scope from the
event's absolute `cwd` and takes one journal snapshot that changes no work state (an existing journal at an older stored schema first runs the pending migration with an automatic backup). If ready
tasks, unexpired active claims, or unapplied (`received`) messages remain,
it exits 2 with a single stderr line such as
`AIQ: runnable work remains: 1 ready task, 1 active claim: [aiq: TASK-7]
"Ship the release notes" (open 2h) — settle finished work: aiq task done
TASK-7 --summary TEXT — or: aiq status`.
After the counts, the line names up to the first three ready tasks — the
task reference carrying the journal's project label, a double-quoted title
truncated to 40 characters, and a coarse ready-age — and ends with the
settle command, whose task ID stays bare so it can be copied straight into
a shell; with claims or messages only, it keeps the
`— run aiq status` tail. A parked `needs_input` message awaits the user, not
the agent, and never blocks stopping, but it is surfaced: a block line
appends a fragment such as `; 2 parked messages await user input` before
the settle tail. Claude Code feeds that line back to the
model and continues the turn, so the model can run the remaining work before
declaring completion.

When nothing is runnable but parked `needs_input` messages remain, the gate
exits 0 with exactly one stderr notice —
`AIQ: no runnable work; 2 parked messages await user input —
aiq inbox list` — instead of full silence, so a session cannot end with a waiting
question unmentioned. Whether stderr from an exit-0 hook is displayed is
host-dependent; Claude Code does not feed it back to the model.

The gate blocks the reader. Runnable work belongs to whichever session may
drain the queue, so the hook derives its own reader identity the same way the
CLI does — configuration or `AIQ_READER`, defaulting to the host and POSIX
session id — and consults the scope's
[reader lease](../contracts/cli-v1.md#reader-lease). A session that only files
work while another live session holds the role stops freely, with one exit-0
stderr notice such as `AIQ: not blocking: runnable work remains (1 ready task)
but reader "host-4242" holds the reader lease — aiq reader status`. In every
other case the gate blocks as above, including when no lease exists at all,
when it has expired or been released, and when its holder is provably dead.
That last case matters: an agent harness can give each shell invocation its
own POSIX session, so a lease routinely outlives the session that took it, and
treating an abandoned lease as an active reader would silently stop enforcing
completion.

The gate honors the host loop guard: when the `Stop` payload carries a truthy
`stop_hook_active` — Claude Code sets it while already continuing because of
a stop hook — the gate exits 0 silently instead of blocking again, so it can
never loop the session. It also exits 0 silently when nothing is runnable
and nothing is parked, or when no journal exists for the scope; it never
creates storage.

The gate fails open, the inverse of capture's fail-visible rule: any error on
the gate path (invalid payload, unresolvable scope, locked or unreadable
journal) exits 0 with one stderr diagnostic, because an AIQ defect must never
block Claude Code from stopping. The normative dispatch and exit semantics
are defined in the [CLI contract](../contracts/cli-v1.md#integrations).

An installation from before the completion gate has no `Stop` group;
`integration check` reports it as ordinary drift, and
`integration install claude --user --repair` adds the missing group and
upgrades the manifest in place. Claude Code snapshots hook configuration at
session start, so restart the session after the repair.

## Externally managed setup

```sh
"$aiq_launcher" integration print claude --user \
  --git-executable "$git_executable"
```

`print` emits a stable `settings.json` hooks fragment and changes no files.
Use it when another tool owns the complete configuration. The packaged agent
bootstrap is separate: `aiq integration print agents` prints the exact
repository-root [`AGENTS.md`](../../AGENTS.md) content.

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

Install is idempotent and refuses symlinks, malformed configuration, a
`settings.json` that sets `disableAllHooks`, and conflicting entries. A changed
owned hook is reported as drift instead of being silently replaced. Review and
apply an explicit repair only after inspecting a new plan.

If the recorded Python or Git executable moves, repair both runtime paths:

```sh
new_launcher=/absolute/path/to/aiq
new_git_executable="$(command -v git)"
"$new_launcher" integration plan claude --user \
  --git-executable "$new_git_executable" --repair
"$new_launcher" integration install claude --user \
  --git-executable "$new_git_executable" --repair
"$new_launcher" integration check claude --user \
  --git-executable "$new_git_executable"
```

Invoking the new AIQ installation selects its Python automatically; the flag
selects Git. Moving or deleting either recorded runtime before repair
interrupts capture.

Uninstall:

```sh
"$aiq_launcher" integration uninstall claude --user
```

It removes only an unchanged, manifest-owned hook. It preserves unrelated later
changes, retains private exact-byte backups, never restores a complete backup,
and is safe to repeat.

Those backups may include the complete pre-existing settings file and are
retained indefinitely. See [Privacy](../privacy.md#integration-backups).

## Scope and limits

The managed target is the user-level `settings.json` only. Hooks configured in
a project's `.claude/settings.json`, `.claude/settings.local.json`, or managed
policy settings are outside AIQ's ownership boundary and are neither inspected
nor changed. `disableAllHooks` in any settings scope disables the installed
hook; AIQ detects it only in the managed user-level file.

Claude Code fires `UserPromptSubmit` only for the prompt that starts a turn.
A message the user sends while a turn is running is surfaced to the model
without the event, so the hook never sees it and nothing is captured. AIQ
cannot close this gap host-side; the packaged bootstrap therefore obligates
the agent to ingest each mid-turn message manually with
`aiq ingest --if-new`, which deduplicates against any later re-capture.

## Verify capture

Capture in a repository requires an initialized repo journal: run
`aiq journal init --scope repo` there first (see [Opt-in scope](#opt-in-scope));
`aiq doctor` warns while capture is inactive. After sending one prompt:

```sh
"$aiq_launcher" inbox list
"$aiq_launcher" journal check
```

The hook is silent on success, uses source `claude`, routes by the event's
absolute `cwd`, and deduplicates a repeated identical event.

If capture is missing, run `integration check` and `aiq doctor`, inspect
`journal path` from the target repository, and verify hooks are enabled in
Claude Code. Claude Code snapshots hook configuration at session start;
restart the session or review `/hooks` after installing.
