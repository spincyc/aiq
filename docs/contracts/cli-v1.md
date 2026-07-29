# CLI JSON protocol v1

Status: alpha contract.

This document defines AIQ's first public machine-facing CLI protocol.

## Common rules

- JSON output mode, selected by configuration or `--json`, produces one
  compact, key-sorted UTF-8 JSON object followed by one newline. The
  `capability` and `integration` command families deliberately avoid
  configuration loading; they select JSON output through `--json` or the
  `AIQ_OUTPUT` environment variable only.
- Success uses standard output only. Failure uses standard error only.
- Every JSON result contains top-level integer `"v": 1`. Payload contract
  versions that also surface as top-level `v` (integration lifecycle and
  manifest results) coincide with the protocol envelope version at v1; a
  future divergence requires a new dedicated envelope field, never a
  reinterpretation of `v`.
- An empty inbox or queue is successful and returns an empty array or `null`.
- Object consumers ignore unknown fields. Required field removal or changed
  meaning requires a new protocol version.
- IDs are opaque strings except task IDs (`TASK-` plus a positive decimal) and
  effects-local aliases.
- Timestamps are RFC 3339 UTC strings. Digests are lowercase SHA-256 hex.
- Human-readable output is terminal-safe but is not versioned.
- Successful `ingest --quiet` intentionally emits nothing, including in JSON
  output mode; failures remain visible.

Scope-aware commands accept `--scope auto|repo|user` and `--cwd PATH`. The
parser additionally exposes an `agent-root` scope choice; it is an internal,
unstable hook and not part of this contract.

| Scope | Resolution |
|---|---|
| `repo` | Require Git to resolve the working directory's common directory |
| `user` | Use `$XDG_STATE_HOME/aiq/journal.sqlite3`, with the platform XDG default when unset |
| `auto` | Use repo scope when resolved; use user scope only when Git confirms the directory is not a repository |

`auto` fails closed when Git is unavailable or repository discovery otherwise
fails. It never treats ownership, permission, execution, or malformed-output
errors as proof that the directory is outside a repository.

## Shared objects

### Scope

| Field | Type | Meaning |
|---|---|---|
| `kind` | `"repo"` or `"user"` | Resolved state scope |
| `root` | string | Canonical scope root |
| `scope_id` | string | Opaque stable local scope identifier |
| `journal_path` | string | Local journal path |

### Claim

| Field | Type | Meaning |
|---|---|---|
| `claim_id` | string | Opaque lease identifier |
| `resource_kind` | `"message"` or `"task"` | Leased resource class |
| `resource_id` | string | Leased message or task |
| `owner_id` | string | Caller-supplied owner |
| `basis_revision` | integer or null | Claimed task revision; null for messages |
| `expires_at` | RFC 3339 string | Lease deadline |

Fence counters, acquisition event sequences, and microsecond storage values are
internal and never appear in protocol v1.

### Task summary

| Field | Type |
|---|---|
| `task_id` | string |
| `revision` | positive integer |
| `state` | `queued`, `ready`, `active`, `blocked`, `done`, `canceled`, or `superseded` |
| `priority` | integer |
| `title` | string |
| `blocked_by` | array of task IDs |
| `waiting_on` | array of task IDs |

Task detail adds `objective`, `parent_task_id`, `dependencies`, `reason`,
`superseded_by_task_id`, `created_at`, and `created_by_message_id`. Nullable
fields are present with `null`. Recorded state, event sequences, allocation
numbers, and embedded claims are internal.

### Message summary

Message summaries contain `message_id`, `received_at`, `source`,
`content_sha256`, `session_id`, `turn_id`, `cwd`, `state`, and `lease_status`.
Nullable metadata remains present as `null`. `content` is absent unless the
command explicitly loads it.

## Command results

The tables list fields in addition to top-level `v`.

| Command | Success fields |
|---|---|
| `config show --json` | `version`, `scope`, `owner`, `lease_seconds`, `snapshot_keep`, `output`, `dev_report_repo`; optional `sources` |
| `config check --json` | `status: "ok"` |
| `doctor --json` | `status: "ok"` or `"failed"`, ordered `checks` of `{check, status, detail}` |
| `journal path --json` | `scope` |
| `journal init --json` | `status: "initialized"`, `scope` |
| `journal check --json` | `status: "ok"`, message/task/application/claim/snapshot counts, `scope` |
| `journal snapshot --json` | `status: "created"`, `snapshot_path`, `removed`, `retained`, `scope` |
| `journal export OUTPUT --json` | `status: "exported"`, `output_path`, format fields, record/byte counts, digest, `scope` |
| `journal destroy --plan --json` | confirmation status, token, inventory totals, targets, `scope` |
| `journal destroy --confirm TOKEN --json` | `status: "destroyed"` or `"already_absent"`, `deleted_files`, `scope` |
| `ingest --json` | `message_id`, `state`, `created`, `scope`; with `--if-new`, adds `deduped` |
| `inbox list --json` | `messages` containing message summaries |
| `inbox claim --json` | `claim`, `message`; both null when none is claimable |
| `inbox apply --json` | application receipt described below |
| `inbox needs-input --json` | `status: "needs_input"`, `message_id`, `claim_id`, `replayed` |
| `inbox fail --json` | `status: "failed"`, `message_id`, `claim_id`, `replayed` |
| `task list --json` | `tasks` containing task summaries |
| `task done --json` | `status: "done"`, `message_id`, `tasks` of `task_id`, `revision`, and `state` |
| `task show --json` | `task` containing task detail |
| `task explain --json` | `explain` containing eligibility detail and `explanation` |
| `task history --json` | `task_id`, `events` newest first, each with `occurred_at`, `type`, `detail` |
| `queue peek --json` | `tasks` containing task summaries |
| `queue next --json` | `items`, each containing separate `task` summary and `claim` |
| `enqueue --json` | `task_id`, `state`, `message_id` |
| `dequeue --json` | identical to `queue next` |
| `list --json` | `tasks` of `task_id`, `revision`, `state`, `priority`, and `title` |
| `claim list --json` | `claims` containing unreleased lease summaries with `status` |
| `claim release --json` | `status: "released"`, `claim_id`, `resource_kind`, `resource_id`, `replayed` |
| `status --json` | `messages`, `tasks`, `claims`, `ready`, `scope` |
| `report --json` | `status: "reported"` or `status: "duplicate"`; both add `task_id`, `message_id`, `scope`; `detail_truncated` marks a truncated objective |
| `capability list --json` | sorted `capabilities`, each with `id`, `version`, `purpose`, and `available` |
| `capability show NAME --json` | capability `id`, `version`, purpose, command, and selected contract |
| `integration list --json` | sorted `integrations`, each with `id`, `version`, and `purpose` |
| `integration print agents --json` | `artifact: "agents"`, `content` |
| `integration print (claude\|codex) --json` | `integration` naming the adapter, `fragment` |
| Integration lifecycle command with `--json` | integration status, action, target, changes, and operation-specific fields |
| `reconcile --user --json` | `status`, `apply`, `integrations`, `journal`, `problems` |

`inbox claim` returns exact message content only in its separate `message`
object. `queue next` does not duplicate a claim inside the task. Event IDs and
event sequences never appear in public receipts.

An application receipt contains:

| Field | Type |
|---|---|
| `status` | `"applied"` |
| `message_id` | string |
| `effects_sha256` | lowercase SHA-256 hex |
| `aliases` | object mapping local aliases to task IDs |
| `tasks` | affected task summaries in ascending task-number order |
| `replayed` | boolean |

The digest is SHA-256 of the effects document encoded as UTF-8 after object
keys are sorted by Unicode code point and separators are reduced to `,` and
`:`. Non-ASCII text is emitted directly without Unicode normalization; JSON
control-character escaping still applies. Effects values contain only strings,
integers, null, arrays, and objects; non-finite numbers are invalid.

## Configuration

Configuration inspection is read-only:

```text
aiq config show [--sources] [--scope SCOPE] [--owner OWNER]
                     [--lease-seconds SECONDS] [--snapshot-keep COUNT]
                     [--cwd PATH] [--no-repo-config] [--json]
aiq config check [--scope SCOPE] [--owner OWNER]
                  [--lease-seconds SECONDS] [--snapshot-keep COUNT]
                  [--cwd PATH] [--no-repo-config] [--json]
```

`show` returns the effective values. With `--sources`, `sources` maps each
setting to `cli`, an `env:NAME`, a configuration path, or `default`. `check`
validates discovered layers without initializing a journal. Configuration
precedence and allowed repository keys are defined in
[`configuration.md`](../configuration.md).

## Doctor

`aiq doctor` summarizes local health with cheap read-only checks:

```text
aiq doctor [--scope SCOPE] [--cwd PATH] [--no-repo-config] [--json]
```

Each check reports `check`, `status`, and `detail`. `status` is `ok`, `warn`,
`fail`, or `skipped`. The stable check order is `python`, `sqlite`, `config`,
`git`, `scope`, `journal`, `journal.deep`, `integration.claude`,
`integration.codex`, and `report`.

- `python` and `sqlite` compare the runtime against the supported minimums.
- `config` validates the effective configuration layers.
- `git` reports Git availability for repository scope resolution.
- `scope` resolves the selected scope and journal location.
- `journal` inspects an existing journal file without opening it for
  writing: file type, permissions, a SQLite quick check, and schema-version
  compatibility. An uninitialized journal is `skipped`, not a failure.
- `integration.claude` and `integration.codex` run the read-only integration
  check only when the adapter's target or recorded state exists; an absent
  integration is `skipped`.

Doctor never mutates local state and never performs remote calls. Deep
journal verification stays explicit: `journal.deep` is always `skipped` and
points to `aiq journal check`, which verifies semantic history and may
migrate supported storage.

Doctor writes its report to standard output even when checks fail. Exit 0
means no check reported `fail`; exit 1 means at least one did. Warnings and
skips do not change the exit code. Failures that prevent producing the
report itself use the standard error envelope and exit codes.

## Export and destruction

Export uses an explicit new output path:

```text
aiq journal export OUTPUT
```

It writes a private deterministic JSONL file, refuses to overwrite any path,
and returns `format: "aiq-journal-jsonl"`, `format_version: 1`, `records`,
`bytes`, `content_sha256`, `output_path`, and `scope`.

Journal destruction is a two-command, state-fenced operation:

```text
aiq journal destroy --plan
aiq journal destroy --confirm TOKEN
```

The plan returns `status` (`confirmation_required` or `already_absent`),
`confirmation_token`, `journal_present`, `files`, `managed_backups`,
`total_bytes`, `targets`, and `scope`. `targets` contains relative managed
paths with kind and size. Confirmation succeeds only against the same resolved,
unchanged inventory. It returns `deleted_files` and status `destroyed` or
`already_absent`. External exports and integration state are outside its
deletion boundary.

## Generic event ingestion

Canonical provider-neutral input uses either exact form:

```text
aiq ingest --event-json FILE
aiq ingest --event-json -
```

`-` reads standard input. The strict event object contains required `v: 1`,
`source`, and nonempty `content`; optional fields are `idempotency_key`,
`session_id`, `turn_id`, and absolute `cwd`. Unknown or duplicate keys,
unsupported versions, invalid UTF-8, and nonstandard JSON numbers fail before
journal mutation.

An event `cwd` overrides command `--cwd` for scope resolution and stored
provenance. An identical idempotent retry returns the original `message_id`
with `created: false`; changed content under the same identity is
`state_conflict`. Size and field limits are documented in
[`integrations/generic.md`](../integrations/generic.md).

## Introspection

Task and claim introspection is read-only, deterministic, and bounded:

```text
aiq task explain TASK_ID
aiq task history TASK_ID [--limit N]
aiq claim list [--owner OWNER] [--resource message|task]
               [--status active|expired] [--limit N]
```

`task explain` reads one consistent snapshot and returns `explain` with:

| Field | Type | Meaning |
|---|---|---|
| `task_id` | string | Explained task |
| `revision` | positive integer | Current task revision |
| `state` | string | Effective task state |
| `recorded_state` | string | Stored task state |
| `prerequisites` | array | Each prerequisite's `task_id`, effective `state`, and `satisfied` |
| `blocked_by` | array of task IDs | Blocking tasks |
| `waiting_on` | array of task IDs | Awaited tasks |
| `claim` | object or null | Active lease: `claim_id`, `owner_id`, `expires_at` |
| `reason` | string or null | Recorded transition reason |
| `superseded_by_task_id` | string or null | Replacement task |
| `explanation` | string | One deterministic single-line summary |

`task history` returns at most `--limit` recorded events for one task
(default 50, between 1 and 1000), newest first. Message content never
appears. Each entry contains:

| Field | Meaning |
|---|---|
| `occurred_at` | RFC 3339 event time |
| `type` | `task.created`, `task.revised`, `task.state_changed`, `task.dependency_added`, `task.dependency_removed`, `claim.acquired`, `claim.released`, `claim.consumed`, `claim.revoked`, or `claim.expired` |
| `detail` | Type-specific object: task events carry the resulting `revision` plus the changed `state`, patched `fields`, or `dependency`; claim events carry `claim_id` plus `owner_id` and `expires_at`, or the release `disposition` |

`claim list` returns at most `--limit` unreleased claims (default 100,
between 1 and 1000) in acquisition order. `--owner`, `--resource`, and
`--status` filters apply before the limit. Each entry contains:

| Field | Meaning |
|---|---|
| Shared claim fields | The [Claim](#claim) object fields |
| `status` | `active` while the lease deadline is in the future; `expired` once the lease is recoverable |

## Status

The status dashboard is read-only and reads one journal snapshot:

```text
aiq status [--scope SCOPE] [--cwd PATH] [--json]
```

| Field | Meaning |
|---|---|
| `messages` | Message counts keyed by `received`, `processing`, `applied`, `needs_input`, and `failed` |
| `tasks` | Effective task-state counts keyed by every task state |
| `claims` | `active`: unreleased, unexpired message and task leases |
| `ready` | At most the five highest-priority ready tasks, each with only `task_id`, `priority`, and `title` |
| `scope` | The resolved [Scope](#scope) object |

A processing message whose lease has expired counts as `received`. Message and
prompt content never appears. A missing journal reports zero counts and an
empty `ready` array without creating storage.

## Workflow shortcuts

The workflow commands are transactional sugar over the message pipeline,
never a bypass: every task mutation still flows through one recorded message
and one atomic effects application, composed inside a single journal
transaction.

```text
aiq enqueue TITLE [--objective TEXT] [--priority N] [--requires TASK-ID ...]
aiq dequeue [--owner OWNER] [--lease-seconds N] [--limit N]
aiq list [--state STATE ...] [--all] [--limit N]
aiq task done TASK_ID [TASK_ID ...] --summary TEXT [--owner OWNER]
aiq ingest --if-new ...
```

`enqueue` persists an auto-generated request message with source `cli`,
claims it, applies one create-task effects document, and marks the message
applied — all in one transaction. Each required task's current revision is
resolved into the document's `expect` internally. Any failure rolls the whole
request back, including the message.

`dequeue` is the ergonomic synonym of `queue next` with identical semantics:
it grants a time-bounded lease and never removes the task. The response shape
is the `queue next` shape.

`list` is a top-level task listing that, unlike `task list`, can include
terminal states: the default shows `queued`, `ready`, `active`, and
`blocked`; `--all` adds `done`, `canceled`, and `superseded`; `--state`
selects states explicitly (`--state` and `--all` are mutually exclusive).
Rows are ordered by task number ascending and bounded by `--limit`
(default 50, between 1 and 1000).

`task done` settles every named task in one transaction: it persists
`--summary` as a message with source `cli`, claims it, and applies one
effects document that transitions all named tasks to `done`, resolving each
task's current revision into `expect` internally and recording the summary as
each transition reason. A task already `active` under the effective owner
completes with its existing claim; a `ready` task is leased inside the same
transaction. The command is all-or-nothing: any ineligible task — unknown,
`queued`, `blocked`, terminal, or `active` under another owner — fails the
whole command naming the offending task, with no partial changes and no
stored message.

`ingest --if-new` compares the exact content, by content hash, against the
unapplied (`received` and `needs_input`) messages in the selected scope
before persisting. On a match it returns the oldest matching `message_id`
with `deduped: true` and `created: false` instead of storing a duplicate;
otherwise it stores normally with `deduped: false`. Message content is never
printed.

`inbox claim` with an explicit `MESSAGE_ID` may also resume a parked
`needs_input` message once its missing input has arrived; the resumed claim
can then apply effects or record a disposition. An unaddressed
`inbox claim` still draws only from `received` messages.

## Dev reports

`aiq report` files an AIQ defect observed in any local repository as one
bug-fix task in the local AIQ development checkout's repository-scope journal:

```text
aiq report --summary TEXT (--detail TEXT|--detail-file FILE|-)
           [--to PATH] [--priority N] [--json]
```

The target is `--to PATH`, else the configured `dev_report_repo`
(configuration or `AIQ_DEV_REPORT_REPO`); unset is `invalid_config`. The
target must be an absolute path to an existing directory inside a Git
repository whose journal is already initialized; `aiq report` never
initializes a journal and never writes to the reporting repository's journal.
Reporting is same-machine only: it writes the target journal directly and
performs no remote calls.

The stored message content is the canonical compact key-sorted JSON of exactly
`aiq_version`, `detail`, and `summary`, and the ingest idempotency key is the
SHA-256 of that content, so identical reports deduplicate across reporting
repositories. `--summary` (at most 200 characters) becomes the task title and
`--detail` (at most 16000 characters) becomes the objective, which keeps the
first 2000 characters; the result includes `detail_truncated` when the
objective was cut, and the deduplicated message always retains the full
detail. `--priority` defaults to 60. The reporting origin is recorded only as
the message `cwd`, with source `dev-report`. A repeated identical report, from
any origin, returns `status: "duplicate"` with the original `message_id` and
`task_id` and creates no second task, including while a concurrent instance is
still applying the first report.

## Integrations

Discovery and manual artifact output are read-only:

```text
aiq integration list
aiq integration print agents
aiq integration print (claude|codex) [--user] [--launcher PATH]
                                     [--git-executable PATH]
```

Without `--json`, `print agents` emits the packaged `AGENTS.md` bytes and
`print` with an adapter name emits that adapter's JSON hook fragment. Their
JSON forms use the fields in the command-results table. `print` never
requires a resolvable AIQ launcher; an explicit `--launcher` remains
accepted.

The supported managed lifecycle is explicitly user-scoped. `INTEGRATION` is
`claude` or `codex`:

```text
aiq integration plan INTEGRATION --user [--launcher PATH]
                                 [--git-executable PATH] [--repair]
aiq integration install INTEGRATION --user [--launcher PATH]
                                    [--git-executable PATH] [--repair]
                                    [--plan-token TOKEN]
aiq integration check INTEGRATION --user [--launcher PATH]
                                  [--git-executable PATH]
aiq integration uninstall INTEGRATION --user
```

| Adapter | Managed target | Event identity |
|---|---|---|
| `claude` | `${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json` | `session_id`, optional `prompt_id`, `cwd`, and content |
| `codex` | `${CODEX_HOME:-~/.codex}/hooks.json` | `session_id`, `turn_id`, `cwd`, and content |

The event identity is the hook idempotency identity: a byte-identical
redelivered event replays the original message, and any difference in that
identity is captured as a new message.

The guidance lifecycle manages one AIQ-owned marked block containing the
packaged `AGENTS.md` bootstrap inside an explicitly selected file:

```text
aiq integration plan guidance --target PATH [--repair]
aiq integration install guidance --target PATH [--repair]
                                 [--plan-token TOKEN]
aiq integration check guidance --target PATH
aiq integration uninstall guidance --target PATH
```

`--target` is required and must be absolute; AIQ never infers a Codex home, a
repository root, or a dotfiles location. Install appends the block between
`aiq-guidance-v1` marker lines, creates the file only when absent, and
preserves unrelated bytes exactly. Markers without an AIQ manifest are
unmanaged and block every operation; an edited owned block is drift and
requires explicit `--repair`. Uninstall removes only the unchanged owned block
and deletes the file only when AIQ created it and nothing else remains. See
[`integrations/guidance.md`](../integrations/guidance.md).

Launcher selection is deterministic:

| Precedence | Candidate |
|---:|---|
| 1 | Explicit absolute `--launcher` |
| 2 | Absolute lexical path of the `aiq` console entry point actually invoked |
| 3 | `aiq` found through `PATH`, converted to a lexical absolute path |

Relative explicit launchers are rejected. Every candidate must be an executable
regular file. AIQ records the lexical absolute shim path without resolving
symlinks. This path identifies the AIQ installation that produced the hook; it
is not the executable used by the hook.

The hook runtime is the absolute `sys.executable` of the invoked AIQ
installation. Its command begins with:

```text
/absolute/python -I -m aiq integration receive INTEGRATION
```

Isolated mode excludes user site-packages and `PYTHON*` environment variables
from module selection.

Git executable selection occurs when `print`, `plan`, `install`, or `check`
runs:

| Precedence | Candidate |
|---:|---|
| 1 | Explicit absolute `--git-executable` |
| 2 | `git` found through the command's current `PATH`, converted to a lexical absolute path |

Relative explicit paths and paths containing control characters are rejected.
The selected path must name an executable regular file and is not resolved
through symlinks. The hook command embeds the absolute Python and Git paths, so
capture never searches the host agent's `PATH` for AIQ, Python, or Git.

Omitting `--user` for `codex`, or `--target` for `guidance`, is invalid.
`plan` and `check` are read-only. Lifecycle
results identify `integration`, `target`, `status`, and `action`; they add
`integration_id`, `changes`, digests, plan token, backup, trust, or ownership
metadata when relevant. Uninstall results always include `integration_id` and
`deleted_file`. Guidance `check` reports `trust: "not_applicable"` because the
owned block contains no hook command to review. `plan_token` lets install
reject a stale reviewed plan. `repair` must be explicit. Install and uninstall
preserve unrelated configuration and refuse ownership drift.

Changing the Python runtime or Git executable makes an installed hook differ
from the desired definition. `check` reports drift, and `plan` or `install`
updates it only with explicit `--repair`. Repairing a moved installation
requires invoking `integration install` from the replacement AIQ installation.
Changing only the launcher identity does not change the hook command. If the
recorded Python or Git executable later becomes unavailable, capture fails
visibly and does not mutate the journal; install with `--repair` records the
replacement runtime paths.

`integration receive INTEGRATION --integration-id ID --git-executable PATH` is
an adapter-only host entry point. The absolute Git path is required. It reads
the host event from standard input, is silent on success, and uses a concise
host-visible error rather than the normal JSON protocol. The same command is
installed under both managed events and dispatches on the payload's
`hook_event_name`:

| Event | Behavior | Exit codes |
|---|---|---|
| `UserPromptSubmit` | Capture the prompt into the journal | 0 success; 1 failure |
| `Stop` | Completion gate over the scope resolved from the payload `cwd` | 0 allow; 2 block |

Capture failure exits 1 for both adapters: a visible single-line stderr
diagnostic that never blocks the host prompt. Capture never exits 2, because
Claude Code treats exit 2 from a `UserPromptSubmit` hook as a blocking error
that erases the user's prompt, and a journal problem must never block
prompting.

The `Stop` completion gate enforces the AGENTS.md practice that no required
runnable work may remain at completion. It performs one read-only journal
snapshot — a missing journal counts as nothing runnable and creates no
storage — and blocks with exit 2 and exactly one stderr line, for example
`AIQ: runnable work remains: 2 ready tasks, 1 active claim — run aiq status`,
when ready tasks, unexpired active claims, or unapplied (`received`)
messages remain and the payload's `stop_hook_active` loop guard is falsy. A
parked `needs_input` message awaits the user, not the agent, and never
counts as runnable work. Both hosts feed that stderr line back to the model
and continue the turn. When the loop guard is set, or nothing is runnable, the
gate exits 0 silently. The gate fails open: any error on the gate path
(unresolvable scope, invalid payload, locked or unreadable journal) exits 0
with a single stderr diagnostic, so an AIQ defect never blocks stopping —
the inverse of capture, which fails visibly with exit 1.

## Post-upgrade reconciliation

AIQ never updates itself; refreshing the host installation is the external
installer's job. After such an upgrade, one local command re-binds AIQ-owned
integration material and inspects, or with `--apply` validates and migrates,
the selected journal state:

```text
aiq reconcile --user [--apply] [--launcher PATH] [--git-executable PATH]
              [--scope SCOPE] [--cwd PATH] [--json]
```

The default run is strictly read-only. For each supported user-level adapter
whose active manifest exists, it plans against the current invocation (the
current Python runtime, and the explicit, recorded, or discovered Git
executable) and reports one entry with `integration`, `status`, `action`, and
`reason`. Adapters without an installed manifest are `skipped`, never failed.
The journal step for the scope selected by `--scope` is a cheap read-only
inspection by default; full `journal check` validation and supported-storage
migration run only with `--apply`. A journal that does not exist is `skipped`.

The top-level result contains:

| Field | Meaning |
|---|---|
| `status` | `ok` when nothing needs attention; `attention` when findings remain |
| `apply` | Whether the run was invoked with `--apply` |
| `integrations` | The per-adapter entries described above |
| `journal` | The journal inspection or check entry for the selected scope |
| `problems` | Remaining findings; empty exactly when `status` is `ok` |

Per-entry `status` is `ok`, `skipped`, `repaired`, `drifted`, `blocked`, or
`failed`. With `--apply`, reconciliation performs `install --repair` only
where the plan reports a safe repair of manifest-owned material; conflicting,
unmanaged, or unsafe targets stay untouched and are reported. Launcher
selection reuses the integration rules, extended by the recorded manifest
launcher before any `PATH` discovery; reconciliation never guesses beyond
those rules and never mutates pipx, venv, Homebrew, or distro-owned package
environments.

The report is always emitted on standard output. Exit status is `0` when
nothing needs attention and `1` when findings remain that reconciliation
could not, or was not allowed to, fix; error envelopes on standard error keep
their usual exit classes.

## Deterministic ordering

| Result | Order |
|---|---|
| Inbox | oldest current lifecycle event first |
| Task list, queue, and status `ready` | priority descending, then creation order |
| Top-level `list` | task number ascending |
| Dependencies, blockers, waiters | task ID ascending |
| Prerequisites in `task explain` | task ID ascending |
| Task history | newest event first |
| Claim list | claim acquisition order |
| Applied tasks | task number ascending |
| Capabilities | capability ID ascending |

## Effects v1

`inbox apply` reads at most 65,536 UTF-8 bytes from `--effects FILE`; `-` means
standard input. The normative structural schema is
[`schemas/effects-v1.schema.json`](../../schemas/effects-v1.schema.json).

Semantic rules not expressible in JSON Schema:

- the document commits completely or not at all;
- numeric fields use JSON integer syntax, not decimal or exponent syntax;
- every referenced existing task has its current revision in `expect`;
- aliases are unique and may be referenced only after their `create`;
- task parent, dependency, and supersession graphs are acyclic;
- a task has at most one update and one transition per document;
- the same dependency edge changes at most once per document;
- active and terminal tasks reject updates and dependency changes;
- terminal tasks are immutable;
- `active` is entered only by a queue claim;
- `done` requires the current task claim;
- blocked, canceled, and superseded transitions require a reason; and
- superseded additionally requires a different, existing replacement task.

Allowed effective-state transitions are:

| From | To |
|---|---|
| `queued` | `ready`, `blocked`, `canceled`, `superseded` |
| `ready` | `queued`, `blocked`, `canceled`, `superseded` |
| `active` | `queued`, `ready`, `blocked`, `done`, `canceled`, `superseded` |
| `blocked` | `queued`, `ready`, `canceled`, `superseded` |
| terminal states | none |

Applying the identical document again with its original message claim returns
the stored receipt with `replayed: true`. A different document, claim, stale
revision, or invalid graph is rejected without partial effects.

See [errors.md](errors.md) for failure classification and
[versioning.md](versioning.md) for compatibility policy.
