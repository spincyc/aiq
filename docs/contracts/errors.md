# CLI errors

Status: alpha contract.

AIQ separates human diagnostics from stable machine classification. Error
messages may improve without changing the error code: a code is chosen at the
raise site that detects the failure and travels with the error, so it is
independent of the wording, punctuation, and interpolated values of the
diagnostic.

Coverage is the whole error hierarchy, not the journal alone. Every raise
site of `JournalError` and of every subclass of it sets its own code — the
integration errors included, whether raised by name, through the shared
engine's `error_class` parameter, or through a spec's `error_class`
attribute. Every code in the table below is pinnable, `not_found`,
`invalid_argument`, `invalid_document`, `not_claimable`, `state_conflict`,
`invalid_config`, and `integration_drift` included, not only the fence and
integrity codes. `JournalErrorRaiseSiteCoverageTests` in
`tests/test_cli_protocol.py` enforces this: it derives the subclass names
from the tree, so a new error class is covered without editing the test, and
it fails on a raise site that sets no code or an unregistered one. Because
coverage is total, AIQ keeps no rule that infers a code from message text: an
error that somehow reaches the CLI without a registered code is reported as an
implementation defect rather than guessed at from its wording.

## Classification precedence

Classification applies exactly one rule, in this order:

1. **An explicit code decides.** If the error carries a `code` registered in
   the exit table, that code and its exit are the answer. This is checked
   first for every `JournalError` subclass. Nothing below can override it.
2. **Class-based rules.** Configuration and event errors, which are not
   journal errors, map to `invalid_config` and `invalid_document`.
3. **Residue.** A `JournalError` with no code, or with a code missing from
   the exit table, is an AIQ defect and reports `internal_error`, never a
   guess.

There is no wording rule. Message text is read by nobody: no substring,
prefix, or phrase participates in classification, and no consumer should
infer a code from message wording. Ordering is still load-bearing.
`HookIntegrationError` and `GuidanceIntegrationError` are `JournalError`
subclasses, so a class-based arm for either one, tested before the explicit
code, would make `code=` inert on exactly the raise sites that set it most.

## JSON form

In JSON output mode, every failure writes exactly one compact JSON object to
standard error and writes nothing to standard output:

```json
{"code":"revision_conflict","error":"task revision changed","status":"error","v":1}
```

`v`, `status`, `code`, and `error` are required. `status` is always `error`.
`code` is stable machine-readable ASCII. `error` is a single-line,
terminal-safe human explanation. Future optional fields may provide structured
details; consumers must ignore unknown fields.

Invocation errors, including missing commands and invalid arguments, honor
JSON mode whether selected by configuration or `--json`. For the `capability`
and `integration` command families, which deliberately avoid configuration
loading, JSON mode is selected by `--json` or the `AIQ_OUTPUT` environment
variable, consistently for successes and failures. Unexpected defects use
`internal_error`; JSON mode never emits a traceback.

Outside JSON mode, AIQ writes a terminal-safe diagnostic to standard error.
Human wording and formatting are not a compatibility surface. The
adapter-only `integration receive` commands instead use their documented
host-visible capture diagnostic: a single-line stderr message with exit 1,
which never blocks the host prompt.

## Exit codes

| Exit | Category | Representative codes |
|---:|---|---|
| 0 | Success, including an empty queue or no claimable message | — |
| 2 | Invalid invocation, configuration, or input document | `invalid_argument`, `invalid_config`, `invalid_document` |
| 3 | Requested resource does not exist | `not_found` |
| 4 | Resource exists but its state or fence rejects the operation | `not_claimable`, `claim_expired`, `claim_mismatch`, `revision_conflict`, `state_conflict`, `contention`, `reader_held` |
| 5 | Journal integrity or schema compatibility failure | `integrity_failed`, `schema_incompatible` |
| 6 | Filesystem, operating-system, or integration failure | `io_error`, `integration_drift`, `unsupported_environment` |
| 70 | Unexpected AIQ implementation defect | `internal_error` |

The exit code is the coarse recovery category; `code` is the precise branch.
Automation should use both and must not parse `error`. `aiq doctor` and
`aiq reconcile --user` additionally exit 1, with no error envelope, when the
report they emit on standard output contains findings.

This taxonomy versions with the distribution, not with the JSON envelope. A
failure that moves from one exit category to another is a breaking
distribution change — a minor version bump plus a release note enumerating the
movements — while the response envelope stays `v: 1` and no capability
`version` changes. Automation should therefore pin classification to the AIQ
version it runs. See
[Exit-code categories](versioning.md#exit-code-categories).

## Stable codes

| Code | Meaning |
|---|---|
| `invalid_argument` | CLI syntax or a bounded scalar argument is invalid |
| `invalid_config` | Declarative configuration is invalid or contradictory |
| `invalid_document` | JSON or an effects document violates its versioned contract |
| `not_found` | A requested message, task, claim, capability, or journal is absent |
| `not_claimable` | A message or task is not currently eligible for a new claim |
| `reader_held` | Another live session holds the scope's single reader role |
| `claim_expired` | The supplied lease expired before the operation committed |
| `claim_mismatch` | A claim does not own the requested resource or revision |
| `revision_conflict` | A task revision differs from the effects document fence |
| `state_conflict` | A requested transition or mutation is invalid in current state |
| `contention` | A concurrent writer held the journal beyond the bounded retry window |
| `integrity_failed` | Stored data fails structural or semantic integrity checks |
| `schema_incompatible` | The journal is newer than or unsupported by this AIQ |
| `io_error` | A required local filesystem, lock, or subprocess operation failed, or journal state is not a private file or directory this user owns |
| `integration_drift` | Installed integration state differs from its manifest |
| `unsupported_environment` | The host lacks a required supported facility |
| `internal_error` | An uncategorized implementation defect escaped normal handling |

Pinning a code at its raise site fixed how a code is carried, not which code
each site deserved. Sites were first pinned to whatever the retired substring
rules already produced, which preserved several inherited accidents; those
have since been corrected, changing the code and exit status of the affected
failures. The correction applied four rules:

- A stored-data violation found by an audit or a read is `integrity_failed`
  at exit 5, alongside the SQLite integrity and foreign-key checks it sits
  beside. This covers every finding of `journal check`, the queue audit, and
  the corrupt-row checks in task history, task loading, and export.
- A filesystem, ownership, permission, or lock precondition on journal state
  is `io_error` at exit 6, not a resource-state conflict.
- A failure that depends only on the submitted document — arity, JSON type,
  enum membership, a required or forbidden field, duplicate keys or aliases,
  encoding — is `invalid_document` at exit 2. A failure that depends on
  current task state, or that expresses a state-machine or graph rule about
  the requested outcome, stays `state_conflict` at exit 4.
- A malformed caller-supplied scalar is `invalid_argument` at exit 2.

`state_conflict` therefore now means what its description says: a requested
transition or mutation that the current state rejects. It is no longer the
residue category it became by default.

The integration raise sites were pinned the same way, to whatever the
substring rules already produced, and three of them carried the same kind of
accident. They are now corrected: a malformed caller-supplied scalar is
`invalid_argument` by the fourth rule above, a resolved path the host cannot
execute is `unsupported_environment`, and a stored manifest that no longer
describes a usable path is `integration_drift`. Each is reachable only
through the integration command family:

| Site | Was | Now |
|---|---|---|
| `--launcher` path contains control characters | `integration_drift`, exit 6 | `invalid_argument`, exit 2 — it is a malformed caller-supplied scalar, and `--git-executable` already reported that |
| Resolved launcher is not an executable file | `integration_drift`, exit 6 | `unsupported_environment`, exit 6 — a missing host facility, as it already was for Git and Python; exit is unchanged |
| Manifest `git_executable` or `python_executable` field is corrupt | `unsupported_environment`, exit 6 | `integration_drift`, exit 6 — the environment is fine, the stored manifest is not; the sibling `launcher` field already reported drift; exit is unchanged |

The launcher wording had matched neither the argument rules nor the
executable rules, so both of its failures fell through to drift; the manifest
wording named Git and Python, so it matched the executable rules it had
nothing to do with. Only the first of the three changes exit category, and it
is enumerated in the release note the rule above requires. The launcher is now
resolved and classified exactly as Git and Python are, and all three manifest
path fields report drift alike.

Retry behavior depends on the code. `revision_conflict` requires rereading task
state. `claim_expired` requires acquiring a new claim. `not_claimable` may be a
normal competing-worker result. `contention` is transient and safe to retry
after a short delay. `reader_held` is not transient in the same way: retrying
succeeds only once the holder releases the role or its lease expires, so a
caller should stop consuming and either keep writing — `ingest` and `enqueue`
stay open — or inspect the holder with `aiq reader status`. The error message
is a single line naming the holder; the structured channel is
`aiq reader status --json`, never extra envelope fields. Integrity and schema
errors require repair or
a compatible AIQ version; they must never be silently retried as mutations.
`io_error` is not transient either: retrying repeats the same filesystem
outcome until the path, its ownership, or its mode is fixed. `state_conflict`
now always describes current state, so a caller that reads state and retries
can make progress; `invalid_document` and `invalid_argument` never become
valid on retry and require changing what was submitted.

## Operation-specific classification

| Operation | Failure | Code |
|---|---|---|
| `ingest --event-json` | Malformed, duplicate-key, unknown-field, oversized, or unsupported event | `invalid_document` |
| Any ingest form | Request fails canonical event validation | `invalid_document` |
| Any ingest form | Idempotency identity reused with different content | `state_conflict` |
| Any journal operation | Journal directory, file, or lock is absent, unopenable, not owned by this user, or not private | `io_error` |
| `journal check` | Stored journal or queue data violates an invariant | `integrity_failed` |
| `journal export OUTPUT` | Output path names no file, an invalid parent, or managed state | `invalid_argument` |
| `journal export OUTPUT` | Output already exists | `state_conflict` |
| `journal export OUTPUT` | Stored rows, table set, or schema version are corrupt | `integrity_failed` |
| `inbox apply`, `enqueue`, `task done` | Effect has the wrong arity or types, an unknown state, a missing or forbidden field, or a duplicate alias or target | `invalid_document` |
| `inbox apply` | Effect requests a transition the task's current state rejects | `state_conflict` |
| `inbox fail`, `inbox needs-input` | Disposition is not a recognized value | `invalid_argument` |
| `inbox claim`, `queue next`, `dequeue` | Another live session holds the reader lease, empty queue included | `reader_held` |
| `reader acquire` | Another live session holds the reader lease | `reader_held` |
| `task done` | A named task is merely `ready` and another live session holds the reader lease | `reader_held` |
| `reader release` | Another live session holds the reader lease | `reader_held` |
| `inbox apply` | Effects document references an unknown local alias | `invalid_document` |
| `journal destroy --confirm` | Missing, wrong, or stale inventory token | `state_conflict` |
| Integration install/uninstall | Owned configuration or manifest has drifted | `integration_drift` |
| Integration install/uninstall | Stored manifest `launcher`, `git_executable`, or `python_executable` is not an absolute path free of control characters | `integration_drift` |
| Integration install | Explicit `--launcher` is relative or contains control characters | `invalid_argument` |
| Integration install | Explicit `--git-executable` is relative or contains control characters | `invalid_argument` |
| Integration install | Launcher cannot be discovered, is unavailable, or is not executable | `unsupported_environment` |
| Integration install | Required Python runtime is unavailable or not executable | `unsupported_environment` |
| Integration install | Git cannot be discovered, is unavailable, or is not executable | `unsupported_environment` |
| Integration install | A required host facility is unavailable | `unsupported_environment` |
| Automatic or repo scope | Git is unavailable or repository discovery fails unexpectedly | `unsupported_environment` |
| Configuration loading | Unknown, forbidden, malformed, or out-of-range setting | `invalid_config` |
| `report` | No `--to` and no configured `dev_report_repo` | `invalid_config` |
| `report` | Target directory or its initialized journal is absent | `not_found` |
| `report` | Over-length `--summary` or `--detail`, or out-of-range `--priority` | `invalid_argument` |

The integration rows above describe `install`, which is the verb that must
resolve an executable before it writes. `plan` and `check` are report-only:
the same conditions — a relative or control-character `--launcher` or
`--git-executable`, an unavailable launcher, Python runtime, or Git, and a
corrupt stored manifest — leave them at exit 0 with
`{"action":"block","status":"unsafe",...}` and a `blocked_reason`, never an
error envelope. `uninstall` accepts neither option and resolves no
executable, so it rejects them as `invalid_argument` from argument parsing
alone; it does read the stored manifest, so a corrupt one fails it with
`integration_drift`.

Read-only empty results are not failures. For the session holding the reader
lease, `inbox claim` returns null claim and message fields, and queue
operations return an empty array, both with exit 0. Emptiness never softens
`reader_held`: a session that is not the reader is told so whether or not work
happens to be waiting. Automatic scope falling back after Git confirms a
non-repository is also a successful resolution, not an error.
