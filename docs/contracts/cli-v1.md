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
- JSON `task_id` values are always bare. The journal's
  [project label](#project-label) is reported once as a top-level `project`
  field and never prefixed onto an ID inside JSON; only human-readable output
  renders the `[label: TASK-19]` form.
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

### Project label

Every journal carries one project label: the name of the repository or
higher-level orchestrating project its tasks belong to. It is a short single
printable line of at most 64 characters, stored in the journal and reported
as the top-level `project` field of `journal path`, `journal init`,
`journal check`, and `status`.

The default is derived from the scope and needs no configuration:

| Scope | Default label |
|---|---|
| `repo` | The repository root directory's name — the parent of the Git common directory, so a journal under `~/git/aiq/.git` is labeled `aiq`. Linked worktrees share the primary repository's journal and therefore its label |
| `user` | `user` |

`aiq journal init --label TEXT` sets the label explicitly and may be re-run to
change it; a rejected label leaves the stored label untouched. Journals
written before labels existed are backfilled with the derived default the
first time they are opened.

Human-readable listings render task references as `[label: TASK-19]` so a
reference stays unambiguous when several repositories are in play. Bare IDs
are kept wherever the printed text is meant to be copied into a command, and
JSON `task_id` values are never prefixed.

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

### Reader lease

Every journal has one scope-level reader role: many sessions may write, and
one at a time may consume. `ingest`, `enqueue`, and `report` are writes and
stay open to everyone. `inbox claim`, `queue next`, and `dequeue` are
dispatch and require the role, which a successful consume takes implicitly
when it is free — today's single-session use therefore needs no new command.
Another live holder is refused with [`reader_held`](errors.md) and exit 4,
even when the queue and inbox are empty, because the truthful answer for a
non-holder is that it is not the reader.

The role is keyed on the `reader` identity, not `owner`: `owner` defaults to
the OS user and so is shared by one person's concurrent sessions, while the
default `reader` is the host plus the POSIX session id, which every process of
one terminal inherits and two terminals never share. Export a single
`AIQ_READER` to let cooperating workers drain one journal on purpose. See
[`configuration.md`](../configuration.md).

| Field | Type | Meaning |
|---|---|---|
| `status` | `absent`, `held`, `stale`, `expired`, or `released` | Lease state at the read instant. `stale` is an unexpired lease whose recorded holder is provably gone, which the next consumer may take |
| `held` | boolean | Whether the lease is live; false for `stale` |
| `self` | boolean or null | Whether the caller's reader identity is the recorded holder; null when the caller supplied none |
| `owner_id` | string or null | Holder's owner at acquisition |
| `reader_id` | string or null | Holder's reader identity |
| `acquired_at` | RFC 3339 string or null | When the role was last taken |
| `expires_at` | RFC 3339 string or null | Lease deadline |
| `expires_in_seconds` | integer or null | Whole seconds remaining, floored at zero |
| `epoch` | positive integer or null | Monotonic count of acquisitions |

Every gated command slides `expires_at` forward; there is no heartbeat.
`inbox apply`, `inbox needs-input`, `inbox fail`, and `claim release` are
ungated and only renew a lease already held: the per-item claim already proves
legitimate consumption of that item, and none of them hands out new work, so
gating them would strand an item claimed before a takeover. Losing the role
never revokes a claim; claims recover on their own schedule.

The role may be taken over when no row exists, when it was released, when it
has expired, or when its holder is provably dead. Takeover advances `epoch`
and leaves every existing claim untouched. `status`, `list`, `queue peek`,
`inbox list`, `claim list`, and the `task` read commands are never gated.

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
| `config show --json` | `version`, `scope`, `owner`, `lease_seconds`, `reader`, `reader_lease_seconds`, `snapshot_keep`, `output`, `dev_report_repo`; optional `sources` |
| `config check --json` | `status: "ok"` |
| `doctor --json` | `status: "ok"` or `"failed"`, ordered `checks` of `{check, status, detail}` |
| `journal path --json` | `project`, `scope` |
| `journal init --json` | `status: "initialized"`, `project`, `scope` |
| `journal check --json` | `status: "ok"`, `project`, message/task/application/claim/snapshot counts, `scope` |
| `journal snapshot --json` | `status: "created"`, `snapshot_path`, `removed`, `retained`, `scope` |
| `journal export OUTPUT --json` | `status: "exported"`, `output_path`, format fields, record/byte counts, digest, `scope` |
| `journal destroy --plan --json` | confirmation status, token, inventory totals, targets, `scope` |
| `journal destroy --confirm TOKEN --json` | `status: "destroyed"` or `"already_absent"`, `deleted_files`, `scope` |
| `ingest --json` | `message_id`, `state`, `created`, `scope`; with `--if-new`, adds `deduped` |
| `inbox list --json` | `messages` containing message summaries |
| `inbox claim --json` | `claim`, `message`; both null when none is claimable, and `reader_acquired` when one was claimed |
| `inbox apply --json` | application receipt described below |
| `inbox needs-input --json` | `status: "needs_input"`, `message_id`, `claim_id`, `replayed` |
| `inbox fail --json` | `status: "failed"`, `message_id`, `claim_id`, `replayed` |
| `task list --json` | `tasks` containing task summaries |
| `task done --json` | `status: "done"`, `message_id`, `tasks` of `task_id`, `revision`, and `state` |
| `task show --json` | `task` containing task detail |
| `task explain --json` | `explain` containing eligibility detail and `explanation` |
| `task history --json` | `task_id`, `events` newest first, each with `occurred_at`, `type`, `detail` |
| `queue peek --json` | `tasks` containing task summaries |
| `queue next --json` | `items`, each containing separate `task` summary and `claim`; `reader_acquired` |
| `enqueue --json` | `task_id`, `state`, `message_id` |
| `dequeue --json` | identical to `queue next` |
| `list --json` | `tasks` of `task_id`, `revision`, `state`, `priority`, and `title` |
| `claim list --json` | `claims` containing unreleased lease summaries with `status` |
| `claim release --json` | `status: "released"`, `claim_id`, `resource_kind`, `resource_id`, `replayed` |
| `reader status --json` | `reader` containing the [Reader lease](#reader-lease) object, `scope` |
| `reader acquire --json` | `status: "acquired"`, `acquired`, `reader` |
| `reader release --json` | `status: "released"`, `replayed`, `reader` |
| `status --json` | `messages`, `tasks`, `claims`, `reader`, `ready`, `blocked`, `scope` |
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
aiq config show [--sources] [--scope SCOPE] [--owner OWNER] [--reader ID]
                     [--lease-seconds SECONDS] [--snapshot-keep COUNT]
                     [--cwd PATH] [--no-repo-config] [--json]
aiq config check [--scope SCOPE] [--owner OWNER] [--reader ID]
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
`git`, `scope`, `journal`, `capture`, `journal.deep`, `integration.claude`,
`integration.codex`, and `report`.

- `python` and `sqlite` compare the runtime against the supported minimums.
- `config` validates the effective configuration layers.
- `git` reports Git availability for repository scope resolution.
- `scope` resolves the selected scope and journal location.
- `journal` inspects an existing journal file without opening it for
  writing: file type, permissions, a SQLite quick check, and schema-version
  compatibility. An uninitialized journal is `skipped`, not a failure.
- `capture` warns when the resolved scope is a repository without an
  initialized journal, where installed hooks capture nothing until
  `aiq journal init --scope repo` opts the repository in.
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

The status dashboard reads one journal snapshot and changes no work state.
Like every command that opens the journal, opening a journal whose stored
schema is older than the installed version first runs the pending schema
migration with an automatic pre-migration backup; deep semantic
verification stays explicit in `aiq journal check`:

```text
aiq status [--scope SCOPE] [--cwd PATH] [--json]
```

| Field | Meaning |
|---|---|
| `messages` | Message counts keyed by `received`, `processing`, `applied`, `needs_input`, and `failed` |
| `tasks` | Effective task-state counts keyed by every task state |
| `project` | The journal's [project label](#project-label) |
| `claims` | `active`: unreleased, unexpired message and task leases |
| `reader` | The [Reader lease](#reader-lease) object reduced to `status`, `held`, `self`, `owner_id`, `reader_id`, and `expires_at`, read from this same snapshot, plus `live` |
| `ready` | At most the five highest-priority ready tasks, each with only `task_id`, `priority`, `title`, and `created_at` |
| `blocked` | At most five blocked tasks in the same order, each with only `task_id`, `priority`, `title`, and `blocked_by` — the failed prerequisite task IDs causing the block, empty for a directly blocked task |
| `scope` | The resolved [Scope](#scope) object |

A processing message whose lease has expired counts as `received`. Message and
prompt content never appears. A missing journal reports zero counts, empty
`ready` and `blocked` arrays, an `absent` reader lease, and the derived project
label without creating storage. `reader.self` compares the recorded holder
against the caller's configured `reader` identity, so one status read answers
both what work remains and whether this session may consume it. `reader.live`
answers the one question a completion gate asks — is some *other* session
provably still draining this queue? — and demands proof of all of it. It is
true only when the lease is `held`, its holder recorded a locator (a lease
carries the holder's host and POSIX session id only when the holder used a
self-derived identity), that host is this one, that session still exists, and
it is not this process's own session. It is false for every other reading:
`absent`, `stale` (a matching host with a vanished session proves the holder
dead, which reads as `stale` rather than `held`), `expired`, `released`, a
holder that recorded no locator because its identity was configured
explicitly, a holder on another host, and a holder occupying this very
session. Unprovable foreignness therefore never stands a gate down.
Human-readable `ready` and `blocked` lines render the task reference
as `[label: TASK-19]`; the `blocked by` causes stay bare IDs.

## Reader role

The reader commands inspect and manage the [reader lease](#reader-lease)
directly. They are deliberately separate from the `claim` family, which keys
on a different identity with a different lifecycle:

```text
aiq reader status [--scope SCOPE] [--cwd PATH] [--json]
aiq reader acquire [--reader ID] [--owner OWNER] [--lease-seconds N]
aiq reader release [--reader ID]
```

`status` is read-only and creates no storage. `acquire` holds the role
without consuming anything, so an idle session can keep the right to drain;
acquiring while already holding renews the same lease and reports
`acquired: false`. `--lease-seconds` overrides `reader_lease_seconds` for that
acquisition. `release` gives the role up: holding nothing, an already released
lease, and an expired lease all replay with `replayed: true` and exit 0, while
another live holder is `reader_held`. There is no force, steal, or revoke of a
live lease; wait for its expiry instead.

The gated commands take no `--reader` flag: the identity comes from
configuration or `AIQ_READER` so one session cannot accidentally consume under
two identities.

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
it grants a time-bounded lease and never removes the task, and it carries the
identical [reader lease](#reader-lease) requirement. The response shape is the
`queue next` shape, including `reader_acquired`, which is true only when that
invocation took the reader role.

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

Single-reader governs dispatch, not settlement. Settling a task already
`active` under the effective owner is pure settlement and stays open to every
session, because every session is obliged to record what it finished. Settling
a merely `ready` task leases it inside the transaction, which is dispatch, and
is `reader_held` while another live session holds the role. `task done` never
takes the role implicitly; it only renews one already held.

`ingest --if-new` compares the exact content, by content hash, against the
messages in the selected scope whose latest state is `received` before
persisting. On a match it returns the oldest matching `message_id` with
`deduped: true` and `created: false` instead of storing a duplicate;
otherwise it stores normally with `deduped: false`. Dedupe exists for
retries and hook races against messages still awaiting interpretation: a
`needs_input` or `failed` twin already consumed its interpretation, so
identical content stores a new message. Message content is never printed.

`inbox claim` with an explicit `MESSAGE_ID` may also resume a parked
`needs_input` message once its missing input has arrived, or reopen a
`failed` message whose disposition was misjudged; the resumed or reopened
claim can then apply effects or record a disposition. An unaddressed
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

An adapter may declare harness-injected prompt markers. A prompt matching
them — for `claude`, after stripping surrounding whitespace it starts with
`<task-notification` or is one whole `<system-reminder>…</system-reminder>`
block — is not a user request: capture skips it with exit 0, ingests
nothing, creates no journal, and reports a distinct
`{"skipped": "injected-notification"}` receipt instead of a capture result.

Repo-scope capture is opt-in by journal presence. A `UserPromptSubmit`
event whose `cwd` resolves to a Git repository without an initialized repo
journal exits 0 silently, captures nothing, and creates no storage; the
receipt is a distinct `{"skipped": "repo-journal-not-initialized"}`. The
unified invariant is that installed hooks never create journal storage;
`aiq journal init --scope repo` is the per-repository opt-in and
`aiq journal destroy` the opt-out. A `cwd` outside any Git repository
resolves to user scope, which keeps auto-initialization, as do explicit
`aiq ingest` and the generic integration.

The `Stop` completion gate enforces the AGENTS.md practice that no required
runnable work may remain at completion. It performs one journal snapshot
that changes no work state — a missing journal counts as nothing runnable
and creates no storage, while opening an existing journal at an older
stored schema first runs the pending migration with an automatic backup —
and blocks with exit 2 and exactly one stderr line, for example
`AIQ: runnable work remains: 1 ready task, 1 active claim: [aiq: TASK-7]
"Ship the release notes" (open 2h) — settle finished work: aiq task done
TASK-7 --summary TEXT — or: aiq status`,
when ready tasks, unexpired active claims, or unapplied (`received`)
messages remain and the payload's `stop_hook_active` loop guard is falsy.
After the counts, the line names up to the first three ready tasks — task
ID, double-quoted title truncated to 40 characters, and a coarse ready-age
such as `(open 5m)`, its age since creation — and ends with the settle command; with claims or
messages only, it keeps the `— run aiq status` tail. A
parked `needs_input` message awaits the user, not the agent, and never
counts as runnable work, but the block line surfaces it: a fragment such as
`; 2 parked messages await user input` is appended before the settle tail.
Both hosts feed that stderr line back to the model
and continue the turn. When the loop guard is set, the
gate exits 0 silently.

Runnable work obligates the session that may drain it, so the gate follows
the [reader lease](#reader-lease). It derives its own reader identity exactly
as the CLI does — configuration or `AIQ_READER`, defaulting to the host and
POSIX session id — and reads the lease from the same snapshot as the counts.
Only one reading stands the gate down: `reader.self` false with `reader.live`
true, meaning the role is held by a session proved to be alive, on this host,
and not this process's own. That session is a writer only, and it stops with
exit 0 and one stderr notice naming the holder, for example
`AIQ: not blocking: runnable work remains (1 ready task) but reader
"host-4242" holds the reader lease — aiq reader status`.
Every other reading blocks exactly as above: the caller holding the lease
itself, no lease at all, an expired or released lease, a lease whose holder
recorded no locator — which is every explicitly configured `reader` or
`AIQ_READER` identity — a holder on another host, a holder occupying this
same session, and — deliberately — a lease whose holder is provably dead.
The bias is conservative because a harness may give each shell invocation its
own POSIX session, so leases outlive their sessions routinely; honoring an
abandoned one would silently stop enforcing completion. Proof of foreignness,
not absence of doubt, is required because a hook process does not inherit the
agent shell's environment: the gate can derive a different identity than the
CLI that took the lease, so an unproven holder may be this very session.
A deliberate shared-`AIQ_READER` fan-out therefore keeps blocking every
participant, which is the safe direction. Nothing runnable is unaffected by
the role: the parked notice and silent allow behave the same for holder and
non-holder. When nothing is runnable but parked `needs_input`
messages remain, the gate exits 0 with exactly one stderr notice —
`AIQ: no runnable work; 2 parked messages await user input —
aiq inbox list` — instead of full silence, so a session cannot end with a waiting
question unmentioned; whether a host displays stderr from an exit-0 hook
is host-dependent. With nothing runnable and nothing parked, the gate
exits 0 silently. The gate fails open: any error on the gate path
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
| Task list, queue, and status `ready` and `blocked` | priority descending, then creation order |
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
