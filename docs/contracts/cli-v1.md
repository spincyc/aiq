# CLI JSON protocol v1

Status: alpha contract.

This document defines AIQ's first public machine-facing CLI protocol.

## Common rules

- JSON output mode, selected by configuration or `--json`, produces one
  compact, key-sorted UTF-8 JSON object followed by one newline.
- Success uses standard output only. Failure uses standard error only.
- Every JSON result contains top-level integer `"v": 1`.
- An empty inbox or queue is successful and returns an empty array or `null`.
- Object consumers ignore unknown fields. Required field removal or changed
  meaning requires a new protocol version.
- IDs are opaque strings except task IDs (`TASK-` plus a positive decimal) and
  effects-local aliases.
- Timestamps are RFC 3339 UTC strings. Digests are lowercase SHA-256 hex.
- Human-readable output is terminal-safe but is not versioned.
- Successful `ingest --quiet` intentionally emits nothing, including in JSON
  output mode; failures remain visible.

Scope-aware commands accept `--scope auto|repo|user` and `--cwd PATH`.

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
| `config show --json` | `version`, `scope`, `owner`, `lease_seconds`, `snapshot_keep`, `output`; optional `sources` |
| `config check --json` | `status: "ok"` |
| `journal path --json` | `scope` |
| `journal init --json` | `status: "initialized"`, `scope` |
| `journal check --json` | `status: "ok"`, message/task/application/claim/snapshot counts, `scope` |
| `journal snapshot --json` | `status: "created"`, `snapshot_path`, `removed`, `retained`, `scope` |
| `journal export OUTPUT --json` | `status: "exported"`, `output_path`, format fields, record/byte counts, digest, `scope` |
| `journal destroy --plan --json` | confirmation status, token, inventory totals, targets, `scope` |
| `journal destroy --confirm TOKEN --json` | `status: "destroyed"` or `"already_absent"`, `deleted_files`, `scope` |
| `ingest --json` | `message_id`, `state`, `created`, `scope` |
| `inbox list --json` | `messages` containing message summaries |
| `inbox claim --json` | `claim`, `message`; both null when none is claimable |
| `inbox apply --json` | application receipt described below |
| `inbox needs-input --json` | `status: "needs_input"`, `message_id`, `claim_id`, `replayed` |
| `inbox fail --json` | `status: "failed"`, `message_id`, `claim_id`, `replayed` |
| `task list --json` | `tasks` containing task summaries |
| `task show --json` | `task` containing task detail |
| `queue peek --json` | `tasks` containing task summaries |
| `queue next --json` | `items`, each containing separate `task` summary and `claim` |
| `claim release --json` | `status: "released"`, `claim_id`, `resource_kind`, `resource_id`, `replayed` |
| `status --json` | `messages`, `tasks`, `claims`, `ready`, `scope` |
| `capability list --json` | sorted `capabilities`, each with `id`, `version`, `purpose`, and `available` |
| `capability show NAME --json` | capability `id`, `version`, purpose, command, and selected contract |
| `integration list --json` | sorted `integrations`, each with `id`, `version`, and `purpose` |
| `integration print agents --json` | `artifact: "agents"`, `content` |
| `integration print codex --json` | `integration: "codex"`, `fragment` |
| Codex lifecycle command with `--json` | integration status, action, target, changes, and operation-specific fields |

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

A processing message whose lease has expired counts as `received`. Message and
prompt content never appears. A missing journal reports zero counts and an
empty `ready` array without creating storage.

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

## Integrations

Discovery and manual artifact output are read-only:

```text
aiq integration list
aiq integration print agents
aiq integration print codex [--launcher PATH] [--git-executable PATH]
```

Without `--json`, `print agents` emits the packaged `AGENTS.md` bytes and
`print codex` emits the JSON hook fragment. Their JSON forms use the fields in
the command-results table.

The supported managed lifecycle is explicitly user-scoped:

```text
aiq integration plan codex --user [--launcher PATH]
                           [--git-executable PATH] [--repair]
aiq integration install codex --user [--launcher PATH]
                              [--git-executable PATH] [--repair]
                              [--plan-token TOKEN]
aiq integration check codex --user [--launcher PATH]
                            [--git-executable PATH]
aiq integration uninstall codex --user
```

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
/absolute/python -I -m aiq integration receive codex
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
capture never searches Codex's `PATH` for AIQ, Python, or Git.

Omitting `--user` is invalid. `plan` and `check` are read-only. Lifecycle
results identify `integration`, `target`, `status`, and `action`; they add
`integration_id`, `changes`, digests, plan token, backup, trust, or ownership
metadata when relevant. `plan_token` lets install reject a stale reviewed
plan. `repair` must be explicit. Install and uninstall preserve unrelated
configuration and refuse ownership drift.

Changing the Python runtime or Git executable makes an installed hook differ
from the desired definition. `check` reports drift, and `plan` or `install`
updates it only with explicit `--repair`. Repairing a moved installation
requires invoking `integration install` from the replacement AIQ installation.
Changing only the launcher identity does not change the hook command. If the
recorded Python or Git executable later becomes unavailable, capture fails
visibly and does not mutate the journal; install with `--repair` records the
replacement runtime paths.

`integration receive codex --integration-id ID --git-executable PATH` is an
adapter-only host entry point. The absolute Git path is required. It reads the
Codex event from standard input, is silent on success, and uses a concise
host-visible error rather than the normal JSON protocol.

## Deterministic ordering

| Result | Order |
|---|---|
| Inbox | oldest current lifecycle event first |
| Task list, queue, and status `ready` | priority descending, then creation order |
| Dependencies, blockers, waiters | task ID ascending |
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
