# Concepts

AIQ separates exact requests from deterministic task state.

```text
message → claim → atomic effects → versioned tasks → ready queue → lease
```

| Object | Purpose |
|---|---|
| Journal | Private SQLite history for one scope |
| Message | Exact captured input plus source metadata |
| Inbox | Messages that still need interpretation or disposition |
| Effects document | One validated, atomic set of task changes |
| Task | Current projection of immutable revisions |
| Dependency | A hard requirement that another task finish first |
| Priority | Soft ordering among otherwise ready tasks |
| Claim | Time-bounded ownership of a message or task |
| Capability | Compact, load-on-demand operation contract |

## Messages and effects

A message is source evidence. AIQ stores it exactly and hides its content from
normal inbox listings. A worker claims the message, interprets it once, then
applies a versioned effects document or records a disposition.

| Message state | Meaning |
|---|---|
| `received` | Available for interpretation |
| `processing` | Held by a live message claim |
| `applied` | Its effects committed |
| `needs_input` | Parked with a reason |
| `failed` | Closed as unprocessable |

An effects application is all-or-nothing. Identical retries with the original
claim return the stored result; conflicting retries fail.

## Tasks and revisions

| Task state | Meaning |
|---|---|
| `queued` | Waiting on at least one unfinished dependency |
| `ready` | Eligible for a queue claim |
| `active` | Held by a live task claim |
| `blocked` | Explicitly blocked or blocked by a failed dependency |
| `done` | Completed with the current task claim |
| `canceled` | Terminal and intentionally stopped |
| `superseded` | Terminal and replaced by another task |

Every task mutation creates a revision. Effects that reference existing tasks
must state their expected revisions; stale documents fail without partial
changes.

Readiness is derived, not manually asserted:

| Dependency result | Dependent result |
|---|---|
| Every dependency is `done` | `ready` |
| A dependency is unfinished | `queued` |
| A dependency is blocked, canceled, or superseded | `blocked` |

Among ready tasks, higher integer priority sorts first. Creation order and task
number provide stable tie-breaking.

## Claims and leases

`inbox claim` reserves source interpretation. `queue next` atomically chooses
and reserves ready tasks. A claim records its owner, fence, and expiry.

Expired claims cannot authorize effects or completion. AIQ recovers expired
work during later claim operations; `claim release` returns uncompleted work
voluntarily.

## Scope

Repository scope stores one journal beside Git's common metadata, so linked
worktrees share work. User scope stores one journal below the user's XDG state
directory. Neither scope is designed for multiple OS users or cross-machine
synchronization.

See [Configuration](configuration.md) for exact selection rules.
