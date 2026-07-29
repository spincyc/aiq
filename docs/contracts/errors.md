# CLI errors

Status: alpha contract.

AIQ separates human diagnostics from stable machine classification. Error
messages may improve without changing the error code.

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
JSON mode whether selected by configuration or `--json`. Unexpected defects
use `internal_error`; JSON mode never emits a traceback.

Outside JSON mode, AIQ writes a terminal-safe diagnostic to standard error.
Human wording and formatting are not a compatibility surface. The
adapter-only `integration receive codex` command instead uses its documented
host-visible capture diagnostic.

## Exit codes

| Exit | Category | Representative codes |
|---:|---|---|
| 0 | Success, including an empty queue or no claimable message | — |
| 2 | Invalid invocation, configuration, or input document | `invalid_argument`, `invalid_config`, `invalid_document` |
| 3 | Requested resource does not exist | `not_found` |
| 4 | Resource exists but its state or fence rejects the operation | `not_claimable`, `claim_expired`, `claim_mismatch`, `revision_conflict`, `state_conflict` |
| 5 | Journal integrity or schema compatibility failure | `integrity_failed`, `schema_incompatible` |
| 6 | Filesystem, operating-system, or integration failure | `io_error`, `integration_drift`, `unsupported_environment` |
| 70 | Unexpected AIQ implementation defect | `internal_error` |

The exit code is the coarse recovery category; `code` is the precise branch.
Automation should use both and must not parse `error`.

## Stable codes

| Code | Meaning |
|---|---|
| `invalid_argument` | CLI syntax or a bounded scalar argument is invalid |
| `invalid_config` | Declarative configuration is invalid or contradictory |
| `invalid_document` | JSON or an effects document violates its versioned contract |
| `not_found` | A requested message, task, claim, capability, or journal is absent |
| `not_claimable` | A message or task is not currently eligible for a new claim |
| `claim_expired` | The supplied lease expired before the operation committed |
| `claim_mismatch` | A claim does not own the requested resource or revision |
| `revision_conflict` | A task revision differs from the effects document fence |
| `state_conflict` | A requested transition or mutation is invalid in current state |
| `integrity_failed` | Stored data fails structural or semantic integrity checks |
| `schema_incompatible` | The journal is newer than or unsupported by this AIQ |
| `io_error` | A required local filesystem or subprocess operation failed |
| `integration_drift` | Installed integration state differs from its manifest |
| `unsupported_environment` | The host lacks a required supported facility |
| `internal_error` | An uncategorized implementation defect escaped normal handling |

Retry behavior depends on the code. `revision_conflict` requires rereading task
state. `claim_expired` requires acquiring a new claim. `not_claimable` may be a
normal competing-worker result. Integrity and schema errors require repair or
a compatible AIQ version; they must never be silently retried as mutations.

## Operation-specific classification

| Operation | Failure | Code |
|---|---|---|
| `ingest --event-json` | Malformed, duplicate-key, unknown-field, oversized, or unsupported event | `invalid_document` |
| Any ingest form | Idempotency identity reused with different content | `state_conflict` |
| `journal export OUTPUT` | Output already exists | `state_conflict` |
| `journal destroy --confirm` | Missing, wrong, or stale inventory token | `state_conflict` |
| Integration install/uninstall | Owned configuration or manifest has drifted | `integration_drift` |
| Integration lifecycle | Explicit `--launcher` is relative | `invalid_argument` |
| Integration lifecycle | Explicit `--git-executable` is relative or contains control characters | `invalid_argument` |
| Integration lifecycle | Required launcher or host facility is unavailable | `unsupported_environment` |
| Integration lifecycle | Required Python runtime is unavailable or not executable | `unsupported_environment` |
| Integration lifecycle | Git cannot be discovered, is unavailable, or is not executable | `unsupported_environment` |
| Automatic or repo scope | Git is unavailable or repository discovery fails unexpectedly | `unsupported_environment` |
| Configuration loading | Unknown, forbidden, malformed, or out-of-range setting | `invalid_config` |

Read-only empty results are not failures. `inbox claim` returns null claim and
message fields, and queue operations return an empty array, both with exit 0.
Automatic scope falling back after Git confirms a non-repository is also a
successful resolution, not an error.
