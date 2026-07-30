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
| Reader lease | Time-bounded right to consume a whole scope's work |
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
| `needs_input` | Parked with a reason until the missing input arrives |
| `failed` | Closed as unprocessable until explicitly reclaimed |

An effects application is all-or-nothing. Identical retries with the original
claim return the stored result; conflicting retries fail.

A parked `needs_input` message is not terminal: claiming it explicitly by
message ID resumes it once the missing input arrives, and the resumed claim
can then apply effects or record a disposition. A `failed` message can
likewise be reopened by an explicit claim when its disposition was
misjudged. An unaddressed claim draws only from `received` messages, and a
parked message awaits the user, so it does not count as runnable agent
work.

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

`inbox claim` reserves source interpretation. `queue next` — with `dequeue`
as its ergonomic synonym — atomically chooses and reserves ready tasks: a
lease is time-bounded ownership, never removal. A claim records its owner,
fence, and expiry, plus the session that took it.

Owner and session are different questions. `owner` defaults to the OS user, so
one person's concurrent terminals claim under the same owner; the recorded
session — the host-supplied identity of the session that claimed, plus the
claiming process's host and POSIX session id, exactly as a reader lease
records its holder's — is what lets a session recognize its own claims among
everyone's. Nothing in the CLI surfaces it directly; the one thing derived
from it is the `claims.active_this_session` count that keeps a session from
stopping on work only it can settle. See
[Session identity](configuration.md#session-identity) for where that identity
comes from and why the POSIX half is not enough on its own.

Transactional shortcuts (`enqueue`, `task done`) compose this pipeline
inside one journal transaction: they record a message, claim it, and apply
one atomic effects document, never bypassing the pipeline.

Expired claims cannot authorize effects or completion. AIQ recovers expired
work during later claim operations; `claim release` returns uncompleted work
voluntarily.

## Many writers, one reader

A claim reserves one item. A reader lease reserves the right to hand items
out at all, for a whole scope.

Any number of sessions may add work: `ingest`, `enqueue`, and `report` are
never gated, and neither is any read. Exactly one session at a time may drain
it: `inbox claim`, `queue next`, and `dequeue` require the scope's reader
lease, and a successful consume takes it implicitly when it is free, so a
single working session never notices the rule. A second concurrent consumer
is refused with `reader_held` — including when the queue is empty, because the
honest answer is that it is not the reader, not that there is no work.

The role is keyed on a session identity, not on `owner`, which defaults to the
OS user and is therefore shared by one person's concurrent terminals. The role
slides forward on every gated command and is taken over once it is released,
expires, or its holder is provably gone.

Single-reader governs dispatch, not settlement. A session that already holds a
claim keeps applying, parking, failing, releasing, and completing that work
after losing the role: the claim itself proves legitimate consumption, and
none of those steps hands out new work. Losing the role never revokes a claim.

The converse is the obligation: giving the role back settles nothing, so a
session that releases while still holding claims of its own has not finished.
`aiq reader release` says so, and the completion gate keeps blocking until
each of those claims is settled or released.

## Scope

Repository scope stores one journal beside Git's common metadata, so linked
worktrees share work. User scope stores one journal below the user's XDG state
directory. Neither scope is designed for multiple OS users or cross-machine
synchronization.

See [Configuration](configuration.md) for exact selection rules.
