# Changelog

Notable user-visible changes are recorded here.

This project uses [Semantic Versioning](https://semver.org/). Public
compatibility covers documented CLI behavior, exit codes, versioned JSON and
effects documents, capability contracts, and integration manifests.

## [Unreleased]

### Added

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

- **Releasing the reader role no longer stands the `Stop` gate down while the
  session still holds claims of its own.** Release is a statement about
  dispatch and deliberately leaves per-item claims in place, so a session that
  dequeued a task, released the role, and stopped left that task claimed and
  unworkable by anyone until its lease expired. Claims now record the session
  that took them — the claiming process's host and POSIX session id, exactly as
  reader leases already do, since `owner` defaults to the OS user and cannot
  separate one person's concurrent sessions — and `status --json` reports the
  new `claims.active_this_session` count beside the scope-wide
  `claims.active`. A released session holding any of its own blocks with
  `AIQ: this session released the reader role but still holds 1 active claim
  of its own (…) — settle finished work: aiq task done TASK_ID --summary TEXT
  — or hand it back: aiq claim release CLAIM_ID — list yours: aiq claim list
  --status active`; releasing with nothing held stands the gate down exactly
  as before. A concurrent session's claim never blocks this one, and a claim
  written before schema 5 records no session and blocks nobody.
  `aiq reader release` reports the same count as `claims_held` and warns on
  stderr rather than refusing, so a mid-item handoff still works. Journal storage
  moves to schema 5, migrating on first open with an automatic pre-migration
  backup; every AIQ installation reaching a migrated journal must be upgraded.
  The new count inherits the reader lease's known limitation: a session is
  identified by its POSIX session id, so on a host that gives every shell
  invocation its own session it reads zero. That is the same condition under
  which `reader.released_by_self` is never true and `aiq reader release`
  matches no lease, so the release stand-down is unreachable there and the
  gate blocks on the plain counts — the refinement fails toward blocking and
  cannot widen the hole. See the known limitation in
  [`cli-v1.md`](docs/contracts/cli-v1.md#status).

### Changed

- **Breaking: error codes and exit statuses are corrected.** Pinning each code
  at its raise site deliberately preserved whatever the old substring matcher
  produced, including classifications that were accidents of wording. Those
  are now fixed, so scripts that branch on `code` or on the exit status of the
  operations below must be updated. The substring matcher is gone: a code is
  never inferred from a message, and an uncoded error reports `internal_error`
  at exit 70 instead of defaulting to `state_conflict`.
  - `journal check` now exits 5 with `integrity_failed` for every stored-data
    violation it finds, instead of exiting 4 with `state_conflict` (or 2, or
    3) for all but the SQLite integrity and foreign-key checks. A failing
    `journal check` is now uniformly exit 5.
  - Filesystem, ownership, permission, and lock failures on journal state now
    exit 6 with `io_error` instead of exiting 4 with `state_conflict`. This
    affects any command that opens the journal, its directory, or its
    lifecycle lock, and the preconditions of `journal export` and
    `journal destroy`. `io_error` was already a documented code; it is now
    reachable from journal errors.
  - Effects-document contract violations — wrong arity, wrong JSON type, an
    unknown task state, a missing or forbidden metadata field, a duplicate
    alias or a duplicate effect target — now exit 2 with `invalid_document`
    instead of exiting 4 with `state_conflict`. `inbox apply`, `enqueue`, and
    `task done` are affected. Genuine state-machine refusals — a terminal or
    active task rejecting a mutation, an invalid transition, a self-dependency
    or self-supersession, a dependency cycle, an already-applied message —
    keep `state_conflict` at exit 4.
  - `inbox fail` and `inbox needs-input` reject an unrecognized disposition
    with `invalid_argument` at exit 2 instead of `state_conflict` at exit 4,
    matching how they already rejected a malformed claim ID.
  - A relative `XDG_STATE_HOME` now reports `unsupported_environment` at
    exit 6 instead of `state_conflict` at exit 4.
  - A relative `--git-executable`, or one containing control characters, now
    reports `invalid_argument` at exit 2 for journal scope resolution, which
    is what the integration commands already reported for the same input.
  - `journal export` on a journal with corrupt rows, an unexpected table set,
    or an unreadable schema version now exits 5 with `integrity_failed`
    instead of exiting 4 or 2.
  - An ingest request that fails canonical event validation now reports
    `invalid_document` at exit 2 in every case. It previously reported
    `invalid_document` or `state_conflict` depending on how the event layer
    happened to word the diagnostic.
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
- The `journal.init` capability descriptor no longer advertises the internal
  `agent-root` scope choice, which `cli-v1.md` has always disowned as an
  unstable hook outside the contract; its command now reads
  `aiq journal init [--scope auto|repo|user] [--label TEXT] [--json]` and its
  capability `version` is 3. Descriptors are what an agent is told to trust in
  place of guessing commands, so advertising the choice taught agents to
  invoke exactly what the contract refuses to support. The parser still
  accepts `--scope agent-root` and `--agent-root PATH` for internal use.
- The packaged agent bootstrap (`AGENTS.md`) now covers the reader lease: one
  session consumes at a time, `reader_held` reports that another session holds
  the role, and `ingest` and `enqueue` stay open to every session.
- **Breaking:** `aiq inbox list --limit` is now bounded at 1000, matching the
  1–1000 range every other reporting listing already enforced. It was the one
  listing with no upper bound, so an unbounded page could materialize an entire
  large journal in memory. A limit above 1000 is now rejected with
  `invalid_argument` (exit 2) rather than clamped, so a caller passing a larger
  value today — `--limit 100000` as a stand-in for "everything" — starts
  failing and must page instead. The default is unchanged at 20.
- Installation guidance now recommends a pinned release tag
  (`@v0.2.0a1`) and labels `@main` the development channel, and the refresh
  instructions no longer suggest `pipx upgrade aiq-workqueue`, which
  re-resolves the ref already recorded for a Git install and therefore never
  moves a tag-pinned install to a new release; `pipx install --force` with the
  new ref does. The release itself is now verified rather than asserted: CI
  runs its full OS and Python matrix on `v*` tag pushes, and a new
  `make release-check [TAG=vX.Y.Z]` fails loudly when `pyproject.toml`,
  `_SOURCE_VERSION`, the newest `CHANGELOG.md` version section, and the tag
  being cut do not all name one version, or when the changelog's section,
  heading, and link-reference structure is malformed.

### Fixed

- The `Stop` completion gate no longer stands down for the session that holds
  the reader lease. A hook process does not inherit the environment of the
  agent's shell, so a gate run could derive a different reader identity than
  the CLI that took the lease and read that session's own lease as a live
  foreign reader's — silently disabling completion enforcement for exactly the
  session doing the work. Standing down now requires proof that somebody else
  is draining the queue: the lease is held, its holder recorded a locator, the
  locator names this host, that session still exists, and it is not the gate
  process's own session. `reader.live` on `status` reports that same proof and
  is no longer true merely because a lease is held. One consequence is
  deliberate and safe: a shared `AIQ_READER` fan-out records no holder
  locator, so the gate now keeps blocking every participant instead of
  standing down for all of them. Who may consume is unchanged — `reader_held`
  refusal, takeover, and release semantics are untouched.
- Stable error codes no longer depend on diagnostic wording. `claim_expired`,
  `claim_mismatch`, `revision_conflict`, `integrity_failed`,
  `schema_incompatible`, and `unsupported_environment` are now set at their
  raise sites instead of being recovered by matching substrings of the human
  message, so rewording a diagnostic cannot silently change a documented code
  and a message that merely contains a matched phrase is no longer
  misclassified. Codes and exit codes are unchanged.
- The remaining stable codes are now set at their raise sites too. `not_found`,
  `invalid_argument`, `invalid_document`, `not_claimable`, and `state_conflict`
  were still recovered by matching the human message across two hundred raise
  sites, ending in a catch-all that returned `state_conflict` for anything
  unmatched, so most diagnostics could not be reworded safely after all. Every
  raise site now carries its code, each pinned to the classification callers
  already received, and a test fails on any new site that omits one. The single
  documented exception is ingest's canonical-event validation, whose diagnostic
  is produced by another layer. Codes and exit codes are unchanged.

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
