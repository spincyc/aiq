# Changelog

Notable user-visible changes are recorded here.

This project uses [Semantic Versioning](https://semver.org/). Public
compatibility covers documented CLI behavior, exit codes, versioned JSON and
effects documents, capability contracts, and integration manifests.

## [Unreleased]

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
- `aiq ingest --if-new`: return the existing unapplied (`received` or
  `needs_input`) message with a `deduped` flag when identical content is
  already pending, instead of storing a duplicate.
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
- Distribution version bumped to `0.1.0a2` for the workflow-command feature
  set.
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

[Unreleased]: https://github.com/spincyc/aiq/commits/main
