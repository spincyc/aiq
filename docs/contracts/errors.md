# CLI errors

Status: alpha contract.

AIQ separates human diagnostics from stable machine classification. Error
messages may improve without changing the error code: a code is chosen at the
raise site that detects the failure and travels with the error, so it is
independent of the wording, punctuation, and interpolated values of the
diagnostic. A residual substring fallback still classifies journal errors
raised outside `aiq.journal` and `aiq.queue`, and those whose text is
forwarded from another layer; it is transitional, and no consumer should
infer a code from message wording.

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
| `io_error` | A required local filesystem or subprocess operation failed |
| `integration_drift` | Installed integration state differs from its manifest |
| `unsupported_environment` | The host lacks a required supported facility |
| `internal_error` | An uncategorized implementation defect escaped normal handling |

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

## Operation-specific classification

| Operation | Failure | Code |
|---|---|---|
| `ingest --event-json` | Malformed, duplicate-key, unknown-field, oversized, or unsupported event | `invalid_document` |
| Any ingest form | Idempotency identity reused with different content | `state_conflict` |
| `journal export OUTPUT` | Output path names no file, an invalid parent, or managed state | `invalid_argument` |
| `journal export OUTPUT` | Output already exists | `state_conflict` |
| `inbox claim`, `queue next`, `dequeue` | Another live session holds the reader lease, empty queue included | `reader_held` |
| `reader acquire` | Another live session holds the reader lease | `reader_held` |
| `task done` | A named task is merely `ready` and another live session holds the reader lease | `reader_held` |
| `reader release` | Another live session holds the reader lease | `reader_held` |
| `inbox apply` | Effects document references an unknown local alias | `invalid_document` |
| `journal destroy --confirm` | Missing, wrong, or stale inventory token | `state_conflict` |
| Integration install/uninstall | Owned configuration or manifest has drifted | `integration_drift` |
| Integration install | Explicit `--launcher` is relative | `invalid_argument` |
| Integration install | Explicit `--git-executable` is relative or contains control characters | `invalid_argument` |
| Integration install | Required launcher or host facility is unavailable | `unsupported_environment` |
| Integration install | Required Python runtime is unavailable or not executable | `unsupported_environment` |
| Integration install | Git cannot be discovered, is unavailable, or is not executable | `unsupported_environment` |
| Automatic or repo scope | Git is unavailable or repository discovery fails unexpectedly | `unsupported_environment` |
| Configuration loading | Unknown, forbidden, malformed, or out-of-range setting | `invalid_config` |
| `report` | No `--to` and no configured `dev_report_repo` | `invalid_config` |
| `report` | Target directory or its initialized journal is absent | `not_found` |
| `report` | Over-length `--summary` or `--detail`, or out-of-range `--priority` | `invalid_argument` |

The integration rows above describe `install`, which is the verb that must
resolve an executable before it writes. `plan` and `check` are report-only:
the same conditions — a relative or control-character `--launcher` or
`--git-executable`, and an unavailable launcher, Python runtime, or Git —
leave them at exit 0 with `{"action":"block","status":"unsafe",...}` and a
`blocked_reason`, never an error envelope. `uninstall` accepts neither
option and resolves no executable, so it rejects them as
`invalid_argument` from argument parsing alone.

Read-only empty results are not failures. For the session holding the reader
lease, `inbox claim` returns null claim and message fields, and queue
operations return an empty array, both with exit 0. Emptiness never softens
`reader_held`: a session that is not the reader is told so whether or not work
happens to be waiting. Automatic scope falling back after Git confirms a
non-repository is also a successful resolution, not an error.
