# Codex integration

The Codex integration captures each `UserPromptSubmit` event before the model
receives it and gates each `Stop` event on remaining runnable work.

| Property | Alpha status |
|---|---|
| Event ingestion | Installed hook calls AIQ's Codex adapter |
| Completion gate | The same command under `Stop` blocks stopping while runnable work remains |
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

`plan` is read-only. Install minimally appends one owned group under each of
`UserPromptSubmit` and `Stop` — both containing the same command, managed
under one manifest and one integration id — preserves unrelated hooks and
top-level fields, and records a private manifest and backups below
`${XDG_STATE_HOME:-$HOME/.local/state}/aiq/integrations/codex/<target-id>/`.

AIQ records the launcher identity, its Python runtime, and Git. The hook invokes
the recorded Python as `-I -m aiq` and passes the recorded Git path. It needs no
shell startup file and cannot be redirected through `PATH`, `PYTHONPATH`, or
`PYTHONHOME`.

Run `/hooks` in Codex and review the installed command before trusting it.
`check` reports `manual_review_required` because AIQ cannot observe or automate
that external trust decision.

## Opt-in scope

Installed hooks never create journal storage. Repo-scope capture is opt-in
by journal presence: in a Git repository whose repo journal is not
initialized, the hook exits 0 silently, captures nothing, and creates no
storage. Run `aiq journal init --scope repo` in the repository to opt in
and `aiq journal destroy` to opt out. Outside any Git repository the event
routes to the user journal, which still auto-initializes on capture.

## Completion gate

The installed command is also registered under the `Stop` event. When Codex
is about to finish a turn, the hook resolves the AIQ scope from the event's
absolute `cwd` and takes one journal snapshot that changes no work state (an existing journal at an older stored schema first runs the pending migration with an automatic backup). If ready tasks,
unexpired active claims, or unapplied (`received`) messages remain, it exits
2 with a single stderr line such as
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
the settle tail. Codex feeds that line back to the model
and continues the turn, so the model can run the remaining work before
declaring completion.

When nothing is runnable but parked `needs_input` messages remain, the gate
exits 0 with exactly one stderr notice —
`AIQ: no runnable work; 2 parked messages await user input —
aiq inbox list` — instead of full silence, so a session cannot end with a waiting
question unmentioned. Whether stderr from an exit-0 hook is displayed is
host-dependent; Codex does not feed it back to the model.

The gate blocks the reader. Runnable work belongs to whichever session may
drain the queue, so the hook resolves a reader identity the same way the CLI
does — configuration or `AIQ_READER` first — and consults the scope's
[reader lease](../contracts/cli-v1.md#reader-lease). What differs is the
default: the hook uses the `session_id` from its own `Stop` payload, which is
authoritative for the session being gated and arrives in the payload rather
than by inheritance. `AIQ_SESSION_ID` still overrides it.
That is what lets the gate and the commands of one session recognize each
other; see [Session identity](../configuration.md#session-identity).

Standing down needs proof that another session is draining the queue: the
lease is held and its recorded holder is demonstrably somebody else — a
different session identity, or a POSIX session on this host that is still
running and is not the hook's own. A session that only files work while such a
reader holds the role stops freely, with one exit-0 stderr notice such as
`AIQ: not blocking: runnable work remains (1 ready task) but reader
"host-4242" holds the reader lease — aiq reader status`.

A session also stands the gate down by saying it is finished.
`aiq reader release` means "I am no longer draining this queue", so when the
lease is `released`, its recorded holder locator names this very session, and
this session holds no live claims of its own, the gate exits 0 with one stderr
notice — `AIQ: not blocking: runnable work remains
(1 ready task) but this session released the reader role — aiq reader status`.
That is how a bounded run (one task, or a fixed batch) ends cleanly with ready
work deliberately left behind. The release must be provably this session's own,
under the same locator discipline: somebody else's release is not this session
declaring anything.

Releasing the role is not settling the work. Release deliberately leaves every
per-item claim in place, so a session that dequeues a task, hands the role
back, and stops would leave that task claimed and unworkable by anyone until
its lease expired. A released session still holding claims of its own
therefore keeps blocking, with a line naming the remedy:
`AIQ: this session released the reader role but still holds 1 active claim of
its own (1 ready task, 1 active claim) — settle finished work: aiq task done
TASK_ID --summary TEXT — or hand it back: aiq claim release CLAIM_ID — list
yours: aiq claim list --status active`.
Only this session's own claims count, proved by the locator each claim
records: a concurrent session claims under the same default owner, and this
session could not settle or release its work honestly. A claim that recorded
no locator is nobody's provable claim and does not block. `aiq reader release`
warns about the same count on stderr when it happens, one step earlier.

In every other case the gate blocks as above: no lease at all, an expired one,
a release by another session, a holder that is provably dead, a holder on
another host, a holder occupying this same session, and any holder that
recorded no locator — which is every explicitly configured `reader` or
`AIQ_READER` identity, releases included.

Those last cases matter. Without proof that the holder is somebody else, an
unproven holder may be this very session, and treating an abandoned lease as
an active reader would silently stop enforcing completion. One consequence is
deliberate: a shared `AIQ_READER` fan-out proves nothing about who is draining
the queue, so the gate keeps blocking every participant.

Everything above needs a session identity that survives between commands.
Codex supplies `session_id` in its hook payloads, which covers the gate. It
exports no session variable AIQ recognizes, so export `AIQ_SESSION_ID` — one
stable value per session — if Codex runs each command in a POSIX session of
its own.
Where no such identity exists, nothing can prove a lease or a claim is its
own: `aiq reader release` matches no lease and honestly reports `not_held`,
neither stand-down is reachable, and the gate simply blocks on the counts,
naming whatever remains. The gate is then strictly more conservative, never
less.

The gate honors the host loop guard: when the `Stop` payload carries a truthy
`stop_hook_active` — Codex sets it while a turn was already continued by a
stop hook — the gate exits 0 silently instead of blocking again, so it can
never loop the session. It also exits 0 silently when nothing is runnable
and nothing is parked, or when no journal exists for the scope; it never
creates storage.

The gate fails open, the inverse of capture's fail-visible rule: any error on
the gate path (invalid payload, unresolvable scope, locked or unreadable
journal) exits 0 with one stderr diagnostic, because an AIQ defect must never
block Codex from stopping. The normative dispatch and exit semantics are
defined in the [CLI contract](../contracts/cli-v1.md#integrations).

An installation from before the completion gate has no `Stop` group;
`integration check` reports it as ordinary drift, and
`integration install codex --user --repair` adds the missing group and
upgrades the manifest in place.

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

Capture in a repository requires an initialized repo journal: run
`aiq journal init --scope repo` there first (see [Opt-in scope](#opt-in-scope));
`aiq doctor` warns while capture is inactive. After sending one prompt:

```sh
"$aiq_launcher" inbox list
"$aiq_launcher" journal check
```

The hook is silent on success, uses source `codex`, routes by the event's
absolute `cwd`, and deduplicates a repeated identical event.

If capture is missing, run `integration check` and `aiq doctor`, inspect
`journal path` from the target repository, and verify that Codex hooks are
enabled. Inline hooks in `config.toml` conflict with the managed
`hooks.json` representation; use
`integration print` when configuration is externally owned. Codex's own
`[hooks.state]` trust records do not conflict, and a repair that changes the
owned hook requires re-trusting it in `/hooks`.
