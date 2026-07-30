# Changelog

Notable user-visible changes are recorded here.

This project uses [Semantic Versioning](https://semver.org/). Public
compatibility covers documented CLI behavior, exit codes, versioned JSON and
effects documents, capability contracts, and integration manifests.

## [Unreleased]

### Added

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

### Changed

- The `Stop` completion gate's block line is now actionable: after the
  counts it names up to the first three ready tasks — task ID,
  double-quoted title truncated to 40 characters, and a coarse ready-age
  such as `(ready 5m)` — and ends with the settle command
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
