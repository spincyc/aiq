# Changelog

Notable user-visible changes are recorded here.

This project uses [Semantic Versioning](https://semver.org/). Public
compatibility covers documented CLI behavior, exit codes, versioned JSON and
effects documents, capability contracts, and integration manifests.

## [Unreleased]

### Breaking changes at a glance

Twelve changes in this release can stop a working setup or a script that
branches on AIQ's output. Each is detailed below; every error-code and
exit-status movement is tabulated under **Exit-code and error-code
migration**, and the storage step under **Upgrading a shared journal**.

- **Error codes and exit statuses are reclassified across 106 raise sites.**
  A failing `aiq journal check` is now uniformly exit 5; filesystem,
  ownership, permission, and lock failures on journal state move to exit 6;
  and effects-document contract violations move to exit 2. `state_conflict`
  stops being the residue category it had become. Automation that branches on
  `code` or on exit status must be updated.
- **The substring classifier is deleted, not merely bypassed.** No rule
  anywhere reads a diagnostic's wording, and an error that reaches the CLI
  without a registered code now reports `internal_error` at exit 70 instead
  of defaulting to `state_conflict` at exit 4.
- **Three integration classifications are corrected, one of them across exit
  categories.** `aiq integration install --launcher` with a control-character
  path moves from `integration_drift` (exit 6) to `invalid_argument`
  (exit 2); the other two change code only.
- **`aiq inbox list --limit` is bounded at 1000**, matching the 1–1000 range
  every other reporting listing already enforced. A larger value is rejected
  with `invalid_argument` (exit 2) rather than clamped, so a caller passing
  `--limit 100000` as a stand-in for "everything" starts failing and must
  page instead. The default is unchanged at 20.
- **`aiq reader release` requires proof of holding the lease.** A `reader_id`
  is a public name, not a credential, so presenting one no longer ends a live
  lease. A live lease this session cannot prove holding is refused with
  `reader_held` at exit 4, and the new `aiq reader release --force` is the one
  deliberate override.
- **A released reader lease now ages out with the lease it was about.**
  Expiry is tested before release, so a released row past its own
  `expires_at` reads `expired` and stands no completion gate down. A year-old
  declaration can no longer switch a gate off.
- **`aiq reader release --json` now reports what it did.** `status` gained
  `already_released`, `forced`, and `not_held` beside `released`, where the
  command previously reported `released` even for a lease it never held, and
  the response gained `released` and `declared` booleans beside `replayed`.
  The envelope stays `v: 1`; see the enum ruling under Changed.
- **The default session identity changed.** A session is identified by
  `AIQ_SESSION_ID`, then by a hook payload's `session_id`, then by a host's
  own variable such as `CLAUDE_CODE_SESSION_ID`, and only then by the
  previous host-plus-POSIX-session derivation. On a host that now supplies an
  identity, every lease and claim recorded under the old default reads as a
  stranger's until it expires or is re-taken.
- **The `Stop` completion gate's stand-down narrowed twice.** Standing down
  now requires positive proof that a demonstrably different and still-live
  session is draining the queue — `reader.live` on `status --json` reports
  that same proof and is no longer true merely because a lease is held — and
  a session that released the role still blocks while it holds active claims
  of its own. A deliberate shared-`AIQ_READER` fan-out consequently keeps
  blocking every participant instead of standing down for all of them.
- **Journal storage moves from schema 4 to schema 6**, two steps in one
  release, migrating on first open with an automatic pre-migration backup.
  Every AIQ installation that reaches the journal — installed hooks included
  — must be upgraded. See **Upgrading a shared journal** below.
- **The `journal.init` capability descriptor is version 3** and no longer
  advertises the internal `agent-root` scope choice, which
  [`cli-v1.md`](docs/contracts/cli-v1.md) has always disowned as an unstable
  hook outside the contract. An agent following the descriptor is no longer
  told to invoke what the contract refuses to support.
- **The `reader.release` capability descriptor is version 2**, correcting a
  command line that omitted `--force`, a purpose that omitted both the proof
  requirement and the completion signal the command records, and an
  idempotency claim that an expired lease replays when it in fact reports
  `not_held`.

### Exit-code and error-code migration

Every failure whose stable `code`, exit status, or both moved in this release
is listed once below. The taxonomy versions with the distribution, not with
the JSON envelope: the envelope stays `v: 1` and no capability `version`
changes for a reclassification. See
[Exit-code categories](docs/contracts/versioning.md#exit-code-categories).

The shape of the correction is four rules, stated in full in
[`errors.md`](docs/contracts/errors.md): a stored-data violation is
`integrity_failed` at exit 5; a filesystem, ownership, permission, or lock
precondition on journal state is `io_error` at exit 6; a failure that depends
only on the submitted document is `invalid_document` at exit 2; and a
malformed caller-supplied scalar is `invalid_argument` at exit 2.
`state_conflict` at exit 4 is left to the state machine it names.

| Operation | Failure | Was | Now |
|---|---|---|---|
| Any journal-opening command | Journal directory, file, backups directory, or lifecycle lock is absent, unopenable, not owned by this user, or not private | `state_conflict`, exit 4 | `io_error`, exit 6 |
| `journal export OUTPUT` | The finished export cannot be published to its output path | `state_conflict`, exit 4 | `io_error`, exit 6 |
| `journal check` | Any stored-journal or queue-audit invariant violation — content hashes, append-only event relationships, task identity and numbering, revisions, applications, claims | `state_conflict`, exit 4; and, for four findings, `invalid_argument` exit 2, `not_found` exit 3, or `claim_mismatch` exit 4 | `integrity_failed`, exit 5 |
| `journal export OUTPUT` | Stored rows, table set, or schema version are corrupt | `state_conflict` exit 4, or `invalid_document` exit 2 | `integrity_failed`, exit 5 |
| Any journal open | Stored scope metadata does not match the journal being opened | `state_conflict`, exit 4 | `integrity_failed`, exit 5 |
| Any read path over stored tasks, claims, or events — `status`, `list`, `queue peek`, `task show`, `task history`, `task explain`, the `inbox` reads | A corrupt stored row, a missing referenced row, or a cycle among stored dependencies is found outside `journal check` | `state_conflict`, exit 4 | `integrity_failed`, exit 5 |
| `inbox apply`, `enqueue`, `task done` | Effect has the wrong arity or JSON type, an unknown task state, a missing or forbidden metadata field, or a duplicate alias or effect target | `state_conflict`, exit 4 | `invalid_document`, exit 2 |
| `inbox apply`, `enqueue`, `task done` | `document.expect` names a malformed task ID | `invalid_argument`, exit 2 | `invalid_document`, exit 2 |
| Any ingest form | The request fails canonical event validation | `invalid_document` exit 2 or `state_conflict` exit 4, decided by how the event layer worded the diagnostic | `invalid_document`, exit 2 |
| `inbox fail`, `inbox needs-input` | The disposition is not a recognized value | `state_conflict`, exit 4 | `invalid_argument`, exit 2 |
| `inbox list` | `--limit` above 1000 | Accepted, exit 0 | `invalid_argument`, exit 2 |
| Journal scope resolution | `--git-executable` is a relative path | `invalid_document`, exit 2 | `invalid_argument`, exit 2 |
| Journal scope resolution | `--git-executable` contains control characters | `state_conflict`, exit 4 | `invalid_argument`, exit 2 |
| Any scope resolution | `XDG_STATE_HOME` is a relative path | `state_conflict`, exit 4 | `unsupported_environment`, exit 6 |
| `integration install` | Explicit `--launcher` path contains control characters | `integration_drift`, exit 6 | `invalid_argument`, exit 2 |
| `integration install` | The resolved launcher is not an executable file | `integration_drift`, exit 6 | `unsupported_environment`, exit 6 |
| `integration install`, `integration uninstall` | Stored manifest `git_executable` or `python_executable` is not an absolute, control-character-free path | `unsupported_environment`, exit 6 | `integration_drift`, exit 6 |
| `reader release` | A live lease the caller can neither prove holding nor show to be abandoned | Release recorded, exit 0 | `reader_held`, exit 4 |
| Any command | An error reaches the CLI carrying no registered code | `state_conflict`, exit 4, or whichever code a substring rule matched | `internal_error`, exit 70 |

Nineteen movements, and the footnotes below name what deliberately did not
move.

1. `integration plan` and `integration check` are report-only and stay so.
   All three integration conditions above — and an unavailable launcher,
   Python runtime, or Git — leave them at exit 0 with
   `{"action":"block","status":"unsafe",...}` and a `blocked_reason`, never
   an error envelope.
2. Every other integration raise site keeps the classification it already
   produced. `code=` had been inert on the whole integration family, because
   the classifier tested `HookIntegrationError` and `GuidanceIntegrationError`
   — both `JournalError` subclasses — before it consulted `code`; the 101
   sites that set no code now set one, each pinned to what the retired
   wording rules produced for it.
3. `invalid_config` and `integration_drift` were documented codes absent from
   the exit table, so pinning one fell through to `internal_error`. Both are
   now registered. No classification changed.
4. `aiq reconcile` no longer decides `skipped` versus `failed` partly by
   searching a diagnostic for `does not exist`, and `aiq report` no longer
   recognizes its two recoverable conflicts (`state_conflict` and
   `not_claimable`) by message substring. Each branch was already reachable
   by code alone, so nothing observable changes.
5. `aiq doctor` and `aiq reconcile --user` still exit 1 with no error
   envelope when the report they write to standard output contains findings.
6. Read-only empty results are still successes, and `reader_held` is still
   returned to a non-reader whether or not work happens to be waiting.

### Upgrading a shared journal

**Journal storage moves from schema 4 to schema 6, in two steps taken in one
release.** Schema 5 records the claiming session's locator on each claim;
schema 6 adds the host-supplied session identity to that locator and to the
reader lease. A journal at schema 4 crosses both on its first open by this
version, which a frozen schema-4 fixture in the test suite exercises
end-to-end. Migration is forward-only: once it has run, every AIQ older than
schema 6 refuses that journal with `schema_incompatible` at exit 5.

**What the operator will see.** Every journal-opening CLI command now
announces the migration on stderr, after writing the pre-migration backup and
before changing anything:

```text
aiq: migrating journal schema 4 -> 6 in place: ~/.local/state/aiq/journal.sqlite3 (scope user, selected by --scope auto fallback outside any repository); forward-only, so AIQ installations older than schema 6 can no longer open this journal; pre-migration backup: ~/.local/state/aiq/backups/pre-migration-v4-to-v6-20260730T101112123456Z-9f2c.sqlite3
```

One line, once, naming the journal, the hop, how the scope was chosen, and a
backup that already exists by the time the line appears — so an unintended
migration is recoverable from the line alone. A scope reached by fallback
says so. The installed capture and completion-gate hooks deliberately stay
silent and keep migrating without a line, holding their documented stdout
silence and one-line stderr budgets.

**What the new columns mean for rows written before the upgrade.** A claim
written before schema 5 carries no locator and a lease or claim written
before schema 6 carries no session identity, so both read as a stranger's:
such a lease is reclaimed by expiry or takeover, and such a claim counts
toward `claims.active` but not `claims.active_this_session`. Nothing is lost
and nothing needs repairing — the gate still names the claim, and the
condition clears the moment the row expires or is re-taken. Absent evidence
always resolves to "not mine" and "not provably foreign", so a pre-migration
row can never stand a completion gate down.

**Everything that reaches one journal is a sharer, and each fails
differently:**

| Sharer | Effect while it is still on the old version |
|---|---|
| Another AIQ on the same machine (a second pipx or virtualenv install) | Every journal-opening command fails with exit 5 and the usual error envelope |
| An installation reaching the same journal through a synced or shared home directory | The same, on that machine |
| An installed `UserPromptSubmit` capture hook bound to the older installation | Capture exits 1 with one `AIQ prompt capture failed` stderr line and records nothing; prompts stop being journaled while the host prompt keeps working |
| That installation's `Stop` completion gate | The gate fails open by design: it exits 0 with one `AIQ completion gate skipped` stderr line and stops enforcing completion. Whether a host displays stderr from an exit-0 hook is host-dependent |

The last two are the dangerous ones, because neither stops the user.

**Pre-flight, before the first open.** This is not hypothetical: during this
batch's development a checkout newer than the installed AIQ ran with its
working directory in a non-repository temporary directory, `auto` resolved to
user scope, and the user's real journal migrated forward — locking out the
installed CLI along with its capture hook and completion gate. Three steps,
in order:

1. **Ask which journal a command would use, before running one that opens
   it:** `aiq journal path --json`. It resolves the path without opening it.
2. **Remember that `auto` outside a repository silently means user scope.**
   `auto` is the default, so a command run from a temporary directory, a
   home-directory shell, or anywhere else outside the repository you had in
   mind selects the *user* journal — the shared one every hook is bound to.
   Name
   the scope explicitly (`--scope repo` or `--scope user`) whenever the
   working directory is not obviously inside the repository you intend.
3. **Inventory every installation that reaches that journal, and upgrade them
   together.** Use the table above as the checklist: every pipx or virtualenv
   install, every machine reaching it through a synced home directory, and
   the capture hook and completion gate bound to each. AIQ cannot see how
   many installations share a journal, so it cannot warn you; the first open
   by the new version is what commits the change for all of them.

Afterwards, run `aiq reconcile --user --apply` to re-bind the AIQ-owned
integration material and validate the migrated journal. If a migration was
not the one you meant,
[`recovery.md`](docs/recovery.md#an-unintended-migration) covers diagnosing
it and rolling back from the backup the announcement named, and
[`versioning.md`](docs/contracts/versioning.md#journal-schema-and-shared-installations)
carries the version-to-schema table.

### Added

- **An in-place journal schema migration now announces itself.** Every
  journal-opening CLI command writes one stderr line before changing
  anything, naming the journal being changed, the schema hop, how the scope
  was selected, and the pre-migration backup, which already exists by the
  time the line appears. A scope reached by fallback says so, so a journal
  the caller never chose is visible at the moment it stops being recoverable
  for free. The installed capture and completion-gate hooks keep migrating
  silently, holding their documented stdout silence and exactly-one-line
  stderr budgets. See **Upgrading a shared journal** above, and
  [`recovery.md`](docs/recovery.md#an-unintended-migration) for diagnosing
  and rolling back an unintended migration from its backup.
- [Using AIQ with an agent](docs/using-with-an-agent.md) documents the system
  for the person who never types an `aiq` command: the setup check, the
  phrases that file work versus run it, the three bounded run modes and their
  stop conditions, and how to read the gate lines, parked-message notices, and
  `reader_held` refusals that appear in an agent transcript.
- `aiq reader release` is now the explicit signal that a session finished a
  bounded run on purpose. When the scope's reader lease is `released` and its
  recorded holder locator proves this very session released it, the `Stop`
  completion gate stops blocking and exits 0 with one notice —
  `AIQ: not blocking: runnable work remains (1 ready task) but this session
  released the reader role — aiq reader status` — so running a single task or a
  fixed batch ends cleanly with ready work deliberately left behind. `status`
  carries the same datum as `reader.released_by_self`. A release by any other
  session, and a release under an explicitly configured identity that records
  no locator, keep blocking exactly as before.
- `status --json` reports a new `claims.active_this_session` count beside the
  scope-wide `claims.active`: of the live claims in this scope, the ones this
  caller is answerable for. It is what lets a completion gate hold a session
  to its own claims and only its own, and it is always less than or equal to
  `claims.active`.
- `aiq reader release --json` reports a new `declared` field — true only when
  the release was proved by the holder locator, which is exactly when
  `reader.released_by_self` becomes true. Read `declared`, not `released`, to
  decide whether a bounded run recorded a stop signal.

### Changed

- **Breaking: error codes and exit statuses are corrected.** Pinning each code
  at its raise site deliberately preserved whatever the old substring matcher
  produced, including classifications that were accidents of wording. Those
  are now fixed, so scripts that branch on `code` or on the exit status of the
  affected operations must be updated; every movement is tabulated under
  **Exit-code and error-code migration** above. The substring matcher is gone:
  a code is never inferred from a message, and an uncoded error reports
  `internal_error` at exit 70 instead of defaulting to `state_conflict`. The
  correction reaches 106 raise sites and covers `journal check`, which is now
  uniformly exit 5 instead of mixing exits 2, 3, 4, and 5 by which invariant
  broke; filesystem, ownership, permission, and lock failures on journal
  state, which move from `state_conflict` to `io_error`; effects-document
  contract violations, which move from `state_conflict` to `invalid_document`
  while genuine state-machine refusals keep `state_conflict`; malformed
  caller-supplied scalars, which move to `invalid_argument`; and ingest's
  canonical-event validation, which now reports `invalid_document` in every
  case instead of varying with the event layer's wording.
- **Breaking: the last three integration classifications are corrected, one of
  them across exit categories.** These were pinned to whatever the substring
  matcher produced and recorded in [`errors.md`](docs/contracts/errors.md) as
  accidents rather than corrected silently; they are now fixed, and the
  matcher itself is deleted rather than merely unreachable, so no rule in the
  classifier reads a message. `aiq integration install` with a `--launcher`
  path containing control characters now reports `invalid_argument` at exit 2
  instead of `integration_drift` at exit 6 — the only exit-category movement,
  and the answer `--git-executable` already gave for the same malformed
  scalar. A resolved launcher that is not an executable file now reports
  `unsupported_environment` instead of `integration_drift`, as Git and Python
  already did. A stored integration manifest whose `git_executable` or
  `python_executable` field is not an absolute, control-character-free path
  now reports `integration_drift` instead of `unsupported_environment`,
  matching its sibling `launcher` field: the manifest is corrupt and the host
  was never consulted. Those last two stay at exit 6 and change only the code.
  `integration plan` and `integration check` remain report-only and still exit
  0 with a `blocked_reason` for all three conditions.
- **Breaking: releasing the reader role no longer stands the `Stop` gate down
  while the session still holds claims of its own.** Release is a statement
  about dispatch and deliberately leaves per-item claims in place, so a session
  that dequeued a task, released the role, and stopped left that task claimed
  and unworkable by anyone until its lease expired. Claims now record the
  session that took them, exactly as reader leases record their holder's, and
  a released session holding any of its own blocks with
  `AIQ: this session released the reader role but still holds 1 active claim
  of its own (…) — settle finished work: aiq task done TASK_ID --summary TEXT
  — or hand it back: aiq claim release CLAIM_ID — list yours: aiq claim list
  --status active`; releasing with nothing held stands the gate down exactly
  as before. A concurrent session's claim never blocks this one, and a claim
  carrying no comparable locator blocks nobody — absent evidence resolves to
  "not mine", which is the safe direction here because counting an
  unattributable claim would block a session on state it can neither settle
  nor release honestly. `aiq reader release` reports the same count as
  `claims_held` and warns on stderr rather than refusing, so a mid-item
  handoff still works. Journal storage moves to schema 5 for the new locator;
  see **Upgrading a shared journal** above.
- **Breaking: `aiq inbox list --limit` is now bounded at 1000**, matching the
  1–1000 range every other reporting listing already enforced. It was the one
  listing with no upper bound, so an unbounded page could materialize an entire
  large journal in memory. A limit above 1000 is now rejected with
  `invalid_argument` (exit 2) rather than clamped, so a caller passing a larger
  value today — `--limit 100000` as a stand-in for "everything" — starts
  failing and must page instead. The default is unchanged at 20.
- **`reader release --json`'s `status` enum is extensible, and gaining values
  is a compatible addition within protocol v1.** This release adds three
  values to it, and the ruling is that the envelope stays `v: 1`: `v` versions
  response *shapes* — which fields exist and what they mean — and the shape did
  not change. [`versioning.md`](docs/contracts/versioning.md#alpha-policy)
  already admits "a new enum value where the contract explicitly declares the
  enum extensible" as a compatible addition, and the root cause was that no
  enum had ever been so declared. [`cli-v1.md`](docs/contracts/cli-v1.md) now
  declares this one extensible in both places a reader meets it — the general
  rules and the `reader release` status table — so the next value added to it
  is compatible by rule rather than by argument. Consumers were already
  required not to treat an unknown enum value as an existing one; a consumer
  that does must branch on `declared`, which is a boolean and answers the
  question a bounded run actually has.
- `versioning.md` now rules where the exit-code taxonomy is versioned, which it
  previously left contradictory: the taxonomy travels with the distribution,
  not with the JSON envelope, so a reclassification like the one above needs a
  minor version bump and a release note enumerating the movements while the
  envelope stays `v: 1` and no capability `version` changes — error
  classification is stated to be no part of a descriptor's command contract.
  The sentence that called an exit-code category change incompatible *within
  protocol v1* is corrected, and the lone remark that 0.2.0a1 moved storage to
  schema 4 is replaced by a version-to-schema table covering every released and
  unreleased version, so an operator can tell which installations a migration
  locks out without reading the source.
- Contract documentation now matches the shipped surface. `cli-v1.md` states
  that both RFC 3339 UTC designators occur (`Z` from the internal clock,
  `+00:00` from stored timestamps) and that consumers must parse RFC 3339
  rather than match a suffix; documents the direct `ingest` inputs,
  `inbox list`, `journal snapshot --keep`, `--no-repo-config`, and the
  lifecycle-free `generic` adapter; tabulates every `--limit` bound, including
  the queue's 1–64 against the 1–1000 of the reporting listings; adds
  `project` to the `status --json` field list; and corrects `reconcile`
  `problems` to the integer count it has always been. `versioning.md` now
  states that a schema migration locks out every other installation sharing
  the journal — hooks included, silently — and how to roll one back.
  Capability descriptors gain the `--lease-seconds`, `ingest`, and config
  override flags they omitted and spell `--state` as repeatable. Behavior,
  JSON shapes, and capability versions are unchanged.
- The `journal.init` capability descriptor now names what the command is for.
  Its command line was `aiq journal init [--json]`, omitting `--scope` and
  `--label` entirely, so an agent asked to set AIQ up could not learn from the
  descriptor that initializing a repository journal is the act that opts that
  repository into hook capture; the purpose now says so and the command line
  carries both options. **Breaking:** the descriptor also stopped advertising
  the internal `agent-root` scope choice, which `cli-v1.md` has always
  disowned as an unstable hook outside the contract, so the command reads
  `aiq journal init [--scope auto|repo|user] [--label TEXT] [--json]`. The
  capability `version` moved from 1 to 3 across the two corrections, one for
  each change to the advertised contract. Descriptors are what an agent
  is told to trust in place of guessing commands, so advertising the choice
  taught agents to invoke exactly what the contract refuses to support. The
  parser still accepts `--scope agent-root` and `--agent-root PATH` for
  internal use.
- The packaged agent bootstrap (`AGENTS.md`) now covers the reader lease: one
  session consumes at a time, `reader_held` reports that another session holds
  the role, and `ingest` and `enqueue` stay open to every session.
- Installation guidance now recommends a pinned release tag and labels `@main`
  the development channel, and the refresh
  instructions no longer suggest `pipx upgrade aiq-workqueue`, which
  re-resolves the ref already recorded for a Git install and therefore never
  moves a tag-pinned install to a new release; `pipx install --force` with the
  new ref does. The release itself is now verified rather than asserted: CI
  runs its full OS and Python matrix on `v*` tag pushes, and a new
  `make release-check [TAG=vX.Y.Z]` fails loudly when `pyproject.toml`,
  `_SOURCE_VERSION`, the newest `CHANGELOG.md` version section, and the tag
  being cut do not all name one version, or when the changelog's section,
  heading, and link-reference structure is malformed.
- The committed project site no longer contradicts that guidance. Its install
  section names no channel at all — the command carries a `TAG` placeholder and
  points at the releases page and the README, so it cannot go stale at the next
  cut — and the refresh note says the new tag must be named. The origin-story
  blog post keeps the `@main` command it was published with, since rewriting a
  dated artifact's commands falsifies it, and gains a dated note recording that
  a pinned tag is now recommended and that hook capture became opt-in per
  repository.

### Fixed

- **A session is now identified by an identity that outlives its commands, so
  `aiq reader release` works on an agent host at all.** Session identity was
  derived from the POSIX session id, on the assumption that one session spans
  many commands and that host hooks run as its children. That is true of a
  terminal and false of Claude Code, which runs every command in a POSIX
  session of its own: the session that took the reader lease was already dead
  when the next command ran, so `aiq reader release` matched nothing, recorded
  nothing, and still reported success — while the gate blocked, having no
  evidence the release was anyone's. Identity is now resolved as `--reader` or
  `AIQ_READER`, then `AIQ_SESSION_ID`, then a host's own variable
  (`CLAUDE_CODE_SESSION_ID`), then the previous host-plus-POSIX-session
  derivation; the `Stop` gate prefers the `session_id` its payload carries over
  anything it can derive, so the gate and that session's commands agree. Leases
  and claims record the host-supplied identity alongside the POSIX pair
  (schema 6), and every "is this mine?" answer — `reader.live`,
  `reader.released_by_self`, `claims.active_this_session` — compares whichever
  evidence both sides carry, so a terminal behaves exactly as before. Rows
  recorded before the upgrade carry no session identity and read as a
  stranger's: such a lease is reclaimed by takeover or expiry, and such a claim
  counts toward `claims.active` only, both self-healing. `aiq reader release`
  now also reports what it did — `released`, `already_released`, or `not_held`
  — instead of reporting `released` for a lease it never held, adds a
  `released` boolean beside `replayed`, and prints one stderr line naming the
  identity it tried when there was nothing to release. Export `AIQ_SESSION_ID`
  on any host that supplies no identity of its own; see
  [`docs/configuration.md`](docs/configuration.md#session-identity).
- **A caller with no session identity can no longer inherit a lease or a claim
  recorded by a session that had one.** Where a lease or claim carried a
  host-supplied session token and the caller had none, the comparison fell
  back to the POSIX pair — but a recorded token means the holder belongs to a
  host that keeps its own sessions, so the process behind the stored session
  id has very likely exited, and the kernel reissues those numbers. A caller
  that happened to land on a reissued id read a stranger's release as its own
  (standing its gate down), read a dead holder as a live foreign session
  (standing it down for nobody), counted a stranger's claim as its own, and
  could release the stranger's live lease. Non-comparable evidence now answers
  "not mine" and "not provably foreign" instead of falling through, matching
  the rule the dead-holder probe already followed. The POSIX pair still
  decides where no token was recorded, so terminals and pre-schema-6 rows are
  unaffected.
- **A configured `reader` no longer fails a bounded run silently.** A lease
  taken under `--reader`, `AIQ_READER`, or `reader` in a configuration file
  records no session locator by design, so `aiq reader release` succeeded
  under such an identity while recording nothing the completion gate could
  read: the command printed `released True` and the gate went on blocking,
  with no way to tell that outcome from the one that works. `reader release`
  now reports a new `declared` field — true only when the release was proved
  by the holder locator, which is exactly when
  `reader.released_by_self` becomes true — and prints one stderr line naming
  the configured identity when a successful release recorded no completion
  signal. [`configuration.md`](docs/configuration.md#reader-identity-and-session-identity)
  now separates the reader identity from the session identity, which were
  presented as one precedence list even though `AIQ_READER` feeds only the
  first, and states that configuring `reader` gives up the per-session
  answers — including the ability to end a bounded run on its own release.
- **Releasing the reader role now requires proof of holding it, and a release
  ages out with the lease it was about.** A `reader_id` is public — `aiq
  reader status` prints it and `--reader` accepts it — but release authorized
  on a matching string, so anyone could run `aiq reader release --reader
  THEIR_ID` against a live lease, and because release leaves the recorded
  holder locator in place, the victim's `Stop` gate then read
  `reader.released_by_self` true and stood down while it still held an active
  claim and had released nothing. Release now demands the same locator proof
  `reader.live` demands: a lease that recorded a holder locator may be
  released only from the session that locator names, and a live lease the
  caller cannot prove holding is refused with `reader_held` at exit 4. A lease
  taken under an explicitly configured `--reader` or `AIQ_READER` records no
  locator by design and is still released by presenting that identity, which
  is shared authority over the role and, as before, records no declaration.
  Separately, a released row never expired — `released` was tested before
  expiry — so a year-old row still stood a gate down, and since POSIX session
  ids restart low after a reboot an unrelated session could inherit a
  stranger's "I am done"; expiry is now tested first, so a released lease
  reads `expired` past its own `expires_at` and stands nothing down. The new
  `aiq reader release --force` is the one deliberate override: it breaks a
  live lease this session cannot prove holding — the only recourse for a lease
  held by a host-identified session that is gone for good, which is never
  proved dead — and it clears the holder locator so it hands no session a
  declaration.
- The `Stop` completion gate no longer stands down for the session that holds
  the reader lease. A hook process does not inherit the environment of the
  agent's shell, so a gate run could derive a different reader identity than
  the CLI that took the lease and read that session's own lease as a live
  foreign reader's — silently disabling completion enforcement for exactly the
  session doing the work. Standing down now requires proof that somebody else
  is draining the queue: the lease is held, its holder recorded a locator, and
  that holder is demonstrably a different session that still exists.
  `reader.live` on `status` reports that same proof and
  is no longer true merely because a lease is held. One consequence is
  deliberate and safe: a shared `AIQ_READER` fan-out records no holder
  locator, so the gate now keeps blocking every participant instead of
  standing down for all of them. Who may consume is unchanged — `reader_held`
  refusal, takeover, and release semantics are untouched.
- The `reader.release` capability descriptor described the command as merely
  giving up the role, at version 1, omitting both that it is the recorded
  signal a bounded run stops on and the `--force` flag, and claiming an
  expired lease replays when it in fact reports `not_held`. The descriptor is
  corrected and is now version 2, so the workflow is discoverable through
  `aiq capability show reader.release`.
- An effects document with an invalid task ID in `document.expect` reported
  `invalid_argument` rather than `invalid_document`, though the failure
  depends only on the submitted document. Both exit 2, so no operator-visible
  behavior changes. `aiq reconcile` also decided `skipped` versus `failed`
  partly by searching the diagnostic for `does not exist`, which contradicted
  the rule that no wording participates in classification; every raise site it
  can reach already pins `not_found`, so the substring test is removed.
- `aiq report` recognized its two recoverable conflicts — an identical report
  filed from another origin repository, and a concurrent instance that claimed
  the message first — by matching a message substring as well as a code. Each
  phrase is built at exactly one raise site and neither can arrive
  interpolated from user content, so the wording arms were unreachable; while
  unreachable, they meant a reworded unrelated diagnostic could have bought
  the already-filed answer. Both branches now decide on `code` alone, as
  everything else does, and the raise side of each is covered by a test.
- **`code=` was inert on every integration error, so install, uninstall, and
  hook-capture failures were still classified by diagnostic wording.** `HookIntegrationError`
  and `GuidanceIntegrationError` are `JournalError` subclasses, but the
  classifier tested those two classes — and their substring rules — before it
  ever consulted `code`, so a code set at an integration raise site was
  discarded and two rewordings of one failure could report different codes and
  exit statuses. An explicit code is now consulted first for every
  `JournalError` subclass, ahead of any class-based or wording-based rule; the
  101 integration raise sites that set no code now set one; and `invalid_config`
  and `integration_drift`, both documented but absent from the exit table, are
  registered, so pinning them no longer falls through to `internal_error`. Every
  site was pinned to the code the previous classifier produced for it, so no
  failure changed code or exit at that step; the three inherited
  misclassifications preserved deliberately there are corrected under Changed
  above. The AST guard that keeps raise sites pinned now derives the error
  classes from the tree instead of recognizing two names, covers the indirect
  `error_class` raise forms the shared integration engine uses, and keys its
  exemptions on repository-relative paths rather than bare filenames that two
  `journal.py` files shared.
- Stable error codes no longer depend on diagnostic wording. `claim_expired`,
  `claim_mismatch`, `revision_conflict`, `integrity_failed`,
  `schema_incompatible`, and `unsupported_environment` are now set at their
  raise sites instead of being recovered by matching substrings of the human
  message, so rewording a diagnostic cannot silently change a documented code
  and a message that merely contains a matched phrase is no longer
  misclassified. Codes and exit codes are unchanged at that step.
- The remaining stable codes are now set at their raise sites too. `not_found`,
  `invalid_argument`, `invalid_document`, `not_claimable`, and `state_conflict`
  were still recovered by matching the human message across two hundred raise
  sites, ending in a catch-all that returned `state_conflict` for anything
  unmatched, so most diagnostics could not be reworded safely after all. Every
  raise site now carries its code, each pinned to the classification callers
  already received, and a test fails on any new site that omits one. The single
  documented exception is ingest's canonical-event validation, whose diagnostic
  is produced by another layer. Codes and exit codes are unchanged at that step.
## [0.2.0a1] - 2026-07-30

### Breaking changes at a glance

Three changes in this release can stop working setups. Each is detailed under
Changed below.

- **Hook capture is opt-in per repository.** Repositories without a journal
  capture nothing; run `aiq journal init --scope repo` to opt one in.
- **One reader per journal.** A second live consumer is refused with
  `reader_held` (exit 4); export a shared `AIQ_READER` for deliberate fan-out.
- **Journal storage moves to schema 4.** Upgrading migrates on first open with
  an automatic pre-migration backup, after which an older AIQ refuses the
  journal. Reinstall every AIQ sharing it — including installed hooks, whose
  capture stops until they run the new version.

### Added

- A scope-level reader lease enforcing many writers, one reader. Any session
  may `ingest`, `enqueue`, and `report`; exactly one at a time may consume
  through `inbox claim`, `queue next`, or `dequeue`, which take the role
  implicitly on a successful consume and slide its expiry forward. New
  `aiq reader status|acquire|release` commands inspect and manage the role
  explicitly, with matching `reader.status`, `reader.acquire`, and
  `reader.release` capability descriptors. New configuration keys `reader`
  (default: this host plus POSIX session id, also settable through
  `AIQ_READER`) and `reader_lease_seconds` (default 1800, also settable
  through `AIQ_READER_LEASE_SECONDS`). New JSON fields, all optional and
  additive: `reader` on `status` and `reader status`, and `reader_acquired`
  on `inbox claim`, `queue next`, and `dequeue` receipts. Schema version 4
  adds a `reader_leases` table; existing journals migrate on first open with
  the usual pre-migration backup.
- Journal-level project labels naming the repository or orchestrating project
  a journal's tasks belong to. The label defaults to the repository root
  directory's name (`user` for user scope), is set explicitly by
  `aiq journal init --label TEXT`, and is backfilled for existing journals on
  first open. Human-readable task references now render as `[aiq: TASK-19]` in
  `aiq task list`, `aiq list`, `aiq status`, `aiq claim list`, `aiq task show`,
  `aiq task history`, and the `Stop`-gate block line; the gate's settle command
  keeps the bare task ID so it stays copy-pasteable. JSON output is additive
  only: a new top-level `project` field on `journal path`, `journal init`,
  `journal check`, and `status`, with `task_id` values never prefixed.
- Bootstrap and Claude Code integration documentation of the mid-turn capture
  gap: hosts deliver mid-turn user messages without a capture event, so agents
  must ingest them manually with `aiq ingest --if-new`.
- Initial local SQLite journal and durable message inbox.
- Deterministic task effects, dependency ordering, leases, and integrity
  checks.
- Compact capability discovery for agents and humans.
- Packaged, context-bounded `AGENTS.md` bootstrap guidance.
- Source-first Python packaging under the `aiq-workqueue` distribution name.
- Installer-neutral pipx and virtual-environment workflows.
- Codex hooks bound to recorded absolute Python and Git executables.
- Codex lifecycle capability descriptors v2 with explicit Git selection.
- Claude Code `UserPromptSubmit` integration managing the user-level
  `settings.json` with `prompt_id` deduplication and non-blocking failure.
- Shared reversible hook-integration engine parameterizing the Claude Code and
  Codex adapters.
- Fail-closed automatic scope selection when Git is unavailable or fails.
- Read-only `aiq status` dashboard reporting bounded message, task, and claim
  counts plus the next five ready tasks.
- Read-only `aiq task explain`, `aiq task history`, and `aiq claim list`
  introspection with bounded deterministic output and no message content.
- Read-only `aiq doctor` summarizing configuration, dependency, journal,
  scope, and integration health; deep integrity stays explicit via
  `aiq journal check`.
- Installer-neutral `aiq reconcile --user` reporting and, with `--apply`,
  repairing AIQ-owned integration drift and validating the selected journal
  after an external upgrade.
- Guidance integration lifecycle managing one reversible AIQ-owned marked
  block of the packaged bootstrap in an explicitly selected file.
- A `make ci` CI-parity target; gitleaks pinned by checksum in CI;
  `tools/verify` runs sanity-check directly; SQLite sidecar suffixes denied by
  the public audit.
- Dev-mode `aiq report`: file an AIQ defect from any local repository as one
  deduplicated bug-fix task in the configured `dev_report_repo` development
  checkout's queue.
- Transactional workflow commands composing the message pipeline in one
  journal transaction, never bypassing it: `aiq enqueue` records, claims,
  and applies one create-task request; `aiq task done` settles one or more
  ready or owned active tasks through one recorded summary message and one
  atomic effects document, all-or-nothing.
- `aiq dequeue` as the ergonomic synonym of `aiq queue next` with identical
  time-bounded lease semantics, and top-level `aiq list` showing tasks in
  task-number order with terminal states available through `--all` or
  `--state`.
- `aiq ingest --if-new`: return the existing unapplied (`received`) message
  with a `deduped` flag when identical content is already pending, instead of
  storing a duplicate. As first written this bullet also claimed
  `needs_input`; dedupe did match that state while the feature was in
  development, but it was narrowed to `received` before release (see Changed
  below), so `received` is what 0.2.0a1 shipped.
- Capability descriptors `task.enqueue`, `task.done`, `task.overview`, and
  `queue.dequeue` for the new operations.

### Changed

- The `Stop` completion gate now blocks the reader rather than every session.
  It derives its own reader identity exactly as the CLI does (configuration or
  `AIQ_READER`, defaulting to the host plus POSIX session id) and stands down
  only when a demonstrably different and still live session holds the scope's
  reader lease, exiting 0 with one stderr notice naming that holder. A chat,
  review, or agent session that only files work therefore stops freely, while
  the session draining the queue must still finish. Every other reading of the
  lease blocks exactly as before: the caller holding it, no lease at all, an
  expired or released lease, and a lease whose holder is provably dead — a
  harness may give each shell invocation its own POSIX session, so leases
  outlive their sessions routinely and honoring an abandoned one would
  silently stop enforcing completion. `status --json` gains an additive
  `reader.live` field carrying that liveness answer.

- Several concurrent workers draining one journal now fail. A session that is
  not the scope's reader gets the new stable error code `reader_held` and
  exit 4 from `inbox claim`, `queue next`, and `dequeue` — including when the
  queue is empty, because the truthful answer is that it is not the reader —
  and from `task done` when it would dispatch a merely `ready` task. A single
  working session is unaffected: the first successful consume takes the role.
  Intentional fan-out stays possible by exporting one shared `AIQ_READER`
  across the cooperating workers. Single-reader governs dispatch, not
  settlement: `inbox apply`, `inbox needs-input`, `inbox fail`,
  `claim release`, and settling a task already active under the caller stay
  open to every session, and losing the role never revokes a held claim.
  Capability `version` rises to 2 for `inbox.claim`, `queue.next`,
  `queue.dequeue`, and `task.done`.

- Contract prose now states honestly that any command or hook opening a
  journal whose stored schema is older than the installed version first
  runs the pending migration with an automatic backup; "never creates
  storage" is scoped to the missing-journal case. The Stop-gate task
  fragment's age is labeled `(open 2h)` — age since task creation — and
  the injected-wrapper skip requires the prompt to be exactly one whole
  block, so wrapper-sandwiched user text is captured, not dropped.

- The `Stop` completion gate now surfaces parked `needs_input` messages:
  a block line appends a fragment such as
  `; 2 parked messages await user input` before the settle tail, and with
  nothing runnable the gate exits 0 with the one-line stderr notice
  `AIQ: no runnable work; 2 parked messages await user input —
  aiq inbox list` instead of full silence (whether a host displays exit-0
  stderr is host-dependent).
- `aiq status` now also lists up to five blocked tasks with the failed
  prerequisites causing each block: a `blocked` array of `task_id`,
  `priority`, `title`, and `blocked_by` in `--json`, and
  `blocked TASK … blocked by TASK` lines in the text output.
- The `Stop` completion gate's block line is now actionable: after the
  counts it names up to the first three ready tasks — task ID,
  double-quoted title truncated to 40 characters, and a coarse ready-age
  such as `(open 5m)`, its age since creation — and ends with the settle command
  `aiq task done TASK --summary TEXT`; with claims or messages only, it
  keeps the `— run aiq status` tail. `status --json` ready entries
  additionally carry `created_at`.
- Repo-scope hook capture is now opt-in by journal presence: a
  `UserPromptSubmit` event in a Git repository without an initialized repo
  journal exits 0 silently with a distinct
  `{"skipped": "repo-journal-not-initialized"}` receipt and creates no
  storage. `aiq journal init --scope repo` opts a repository in;
  already-sprouted repo journals stay opted in until `aiq journal destroy`.
  User scope, explicit `aiq ingest`, and the generic integration keep
  auto-initialization, and `aiq doctor` gains a `capture` finding that warns
  where prompt capture is inactive.
- Bootstrap `AGENTS.md` word budget raised from 150 to 200; the guidance now
  notes the `aiq report` prerequisite (`dev_report_repo`) and that installed
  hooks enforce completion recording at session stop.
- `aiq reconcile --user` defaults to a strictly read-only journal inspection;
  validation and migration now require `--apply`. `integration print` no
  longer requires a resolvable AIQ launcher.
- `aiq report` duplicates return the original `task_id`, and results flag
  truncated objectives with `detail_truncated`. Uninstall results add
  `integration_id` and `deleted_file`; guidance checks report
  `trust: "not_applicable"`.
- Hook capture failure now exits 1 for both adapters and never blocks the
  host prompt; hook dedup identity now covers session, turn identity, working
  directory, and content, so only byte-identical redelivery replays.
- New retryable `contention` error code (exit 4) for journal write
  contention; `not_found` and `not_claimable` are now code-driven. Internal
  engine and journal read-path consolidation.
- A `failed` message is no longer terminally unclaimable: an explicit
  `inbox claim MESSAGE_ID` reopens it, and the reopened claim can then
  apply effects or record a disposition. An unaddressed claim still draws
  only from `received` messages.
- `aiq ingest --if-new` now deduplicates only against messages whose latest
  state is `received`: a `needs_input` or `failed` twin already consumed
  its interpretation, so identical content stores a new message instead of
  being absorbed into the parked or failed one.

### Fixed

- `aiq reader status` and the `reader` block of `aiq status` report an
  unexpired lease whose holder is provably gone as `stale` rather than
  `held`, so an abandoned lease no longer shows a free queue as owned.
  Claim, release, and takeover already decided liveness for themselves
  and are unchanged.
- Hook capture no longer loses a message when the journal is busy: lock
  acquisition on the capture path is bounded well below the host's hook
  timeout and reports a retryable `contention` failure with one stderr
  line, instead of blocking until the host kills the hook silently.
- Claude Code capture no longer ingests harness-injected prompts as user
  messages: a prompt that starts with `<task-notification` or is one whole
  `<system-reminder>` block (after stripping surrounding whitespace) is
  skipped with exit 0, no journal write, and a distinct
  `{"skipped": "injected-notification"}` receipt.
- A parked `needs_input` message is claimable again through an explicit
  `inbox claim MESSAGE_ID`, so it can resume once the missing input
  arrives and then be applied or failed; previously parked messages were
  terminally unclaimable (TASK-25).
- The Stop-gate hook no longer counts `needs_input` messages as runnable
  work: they await the user, not the agent, so they no longer block the
  host from stopping (TASK-25).
- Codex `[hooks.state]` trust records in `config.toml` no longer block the
  integration lifecycle as inline-hook conflicts.
- Hook-integration manifests survive hook-template changes, integration-id
  markers match as exact command tokens, and explicit repair adopts a hook
  left unmanaged by an interrupted install.
- Deterministic idempotent-replay event selection after claim recovery;
  SQLite contention and integrity failures translate to `JournalError`; a
  snapshot-prune race no longer raises.
- Unknown effects aliases raise `JournalError` instead of `KeyError`; task
  read paths use one WAL snapshot; `document.v` requires exact integer `1`;
  `apply_effects` enforces the canonical byte cap on pre-parsed documents.
- Machine-readable `JournalError` codes classify export-path and
  effects-alias errors as exit 2; the JSON envelope version invariant is
  pinned; capability and integration commands honor `AIQ_OUTPUT=json`.

[Unreleased]: https://github.com/spincyc/aiq/compare/v0.2.0a1...HEAD
[0.2.0a1]: https://github.com/spincyc/aiq/releases/tag/v0.2.0a1
