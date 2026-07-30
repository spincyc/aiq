# Versioning

Status: alpha contract.

AIQ versions its distribution, machine protocol, effects documents, capability
descriptors, and integration manifests independently. The journal storage
schema is versioned separately again. It is not a caller-facing API — no
command reports it and no consumer may read the tables — but it is not
invisible either: it decides which AIQ installations can open a given journal.
See [Journal schema and shared installations](#journal-schema-and-shared-installations).

| Surface | Version location | Compatibility rule |
|---|---|---|
| Python distribution | `aiq --version` | Semantic Versioning |
| CLI JSON protocol | top-level `v` | A changed or removed field requires a new protocol version; `v` versions response shapes, not exit codes |
| CLI exit-code taxonomy | no version of its own; it travels with `aiq --version` | Moving a failure between exit categories is a breaking distribution change: a minor version bump and a release note enumerating the movements. See [Exit-code categories](#exit-code-categories) |
| Effects document | document `v` | Accepted syntax and meaning are immutable within a version |
| Capability descriptor | catalog `v` and capability `version` | A changed command contract — purpose, command line, mutation, or idempotency — increments the capability version; error classification is not part of a descriptor |
| Integration manifest | manifest `v` | Readers reject unsupported future versions without mutation |
| Journal storage | internal metadata | AIQ migrates supported journals forward on open; callers must not inspect tables. Migration is one-way and locks out older installations |

## Alpha policy

Before 1.0, a breaking package change increments the minor distribution
version. AIQ still does not silently reinterpret a versioned machine contract:
a breaking CLI response or effects change receives a new contract version.
Patch releases do not break documented contracts.

The following are compatible additions within CLI protocol v1:

- a new command or capability;
- a new optional object field;
- a new stable error code within an existing exit-code category; or
- a new enum value where the contract explicitly declares the enum extensible.

Consumers must ignore unknown object fields. They must not treat unknown enum
values as an existing value. Removing a field, changing its type or meaning,
or changing array order is incompatible and requires a new protocol version.
Changing an exit-code category is a breaking change too, but not to the
envelope: it is a breaking distribution change, requiring a minor version bump
and a release note that enumerates the movements, and it is never silent.

Effects inputs are strict rather than extensible. Unknown fields, operations,
and tuple members are rejected. Any accepted syntax or semantic change that is
not a defect correction requires a new effects version. AIQ may continue to
accept old effects versions after introducing a new one.

### Exit-code categories

The exit-code taxonomy in [`errors.md`](errors.md) versions with the
distribution, not with the JSON protocol envelope. Moving a failure from one
exit category to another — its stable `code`, its exit status, or both —
requires a minor distribution bump under the alpha policy above and a release
note naming every affected operation with its old and new classification. The
envelope stays `v: 1`, and a correction that ships without that release note
is a defect in the release, not a compatible change.

The envelope's job is the response shape. `v` tells a consumer which fields
exist and what they mean, and a consumer's `v == 1` check asks exactly that
question. An exit code is not a field of the response. Raising the envelope to
v2 to repair an error-classification accident would therefore break every
consumer's version check in order to fix something that check does not cover,
and would require publishing a second copy of the entire CLI contract
differing from the first in no response shape at all. The distribution version
is what a caller already reads to know which classifications it is getting, so
that is where the taxonomy is versioned.

For the same reason, error classification is not part of a capability
descriptor's command contract. A descriptor states how to invoke a command and
what the command does; which exit category a failure lands in is a
cross-cutting property of the CLI, documented once in `errors.md` rather than
per command. A reclassification therefore does not increment any capability
`version`, and a capability `version` must never be read as a promise about
exit codes.

Automation should pin classification the way it pins everything else in alpha:
to a distribution version, verified against the version it runs.

## Journal schema and shared installations

A journal is shared local state, not per-installation state. Every process that
opens one must understand its stored schema: the CLI, the installed host
capture hooks, and the `Stop` completion gate. Opening a journal whose stored
schema is older than the installed version runs the pending migration in place,
after writing an automatic pre-migration backup. Migration is forward-only —
AIQ has no downgrade path — and it changes the file for every installation at
once.

Journal access itself fails closed: once a journal has been migrated, any AIQ
older than that schema refuses it entirely with
[`schema_incompatible`](errors.md) and exit 5. What differs is how loudly each
surface reports that, and it covers more than the terminal an upgrade was run
in:

| Sharer | Effect of an unupgraded installation |
|---|---|
| Another AIQ on the same machine (a second pipx or virtualenv install) | Every journal-opening command fails with exit 5 and the usual error envelope |
| An installation reaching the same journal through a synced or shared home directory | The same, on that machine |
| An installed `UserPromptSubmit` capture hook bound to the older installation | Capture exits 1 with one `AIQ prompt capture failed` stderr line and records nothing; prompts stop being journaled while the host prompt keeps working |
| That installation's `Stop` completion gate | The gate fails open by design, so a schema refusal is an error on the gate path: it exits 0 with one `AIQ completion gate skipped` stderr line and stops enforcing completion. Whether a host displays stderr from an exit-0 hook is host-dependent |

The capture and gate rows are the dangerous ones: neither stops the user, so a
stale installation degrades quietly. Every schema bump triggers all of it, and
0.2.0a1 is the release that last did. An installation understands exactly one
schema — the one its own code declares — migrating an older journal forward on
open and refusing a newer one outright, so the version an installation reports
answers which journals it can still open:

| `aiq --version` | Journal schema | Notes |
|---|---:|---|
| 0.1.0a1 | 2 | the first packaged storage |
| 0.1.0a2 | 2, then 3, then 4 | schema 3 added a claims lookup index; schema 4 added the `reader_leases` table |
| 0.2.0a1 | 4 | the release that shipped schema 4 |
| next release | 5, then 6 | schema 5 records the claiming session's locator on each claim; schema 6 adds the host-supplied session identity to that locator and to the reader lease |

Only 0.2.0a1 and later answer the question by number. 0.1.0a1 and 0.1.0a2 were
never released: the version stayed at 0.1.0a2 across both migrations, so an
installation reporting it may hold schema 2, 3, or 4 depending on the commit it
was installed from. Treat any 0.1.x installation as stale and upgrade it rather
than reasoning about which schema it reached.

The remedy is to upgrade every AIQ installation that reaches the journal and
then re-bind the AIQ-owned material with `aiq reconcile --user --apply`, which
reports each adapter it could not repair. Nothing already recorded is lost by
the migration itself; what is lost is whatever an unupgraded hook failed to
capture in the meantime.

### Announcing a migration

Every journal-opening CLI command announces the migration it is about to run,
on stderr, before the first schema statement executes:

```text
aiq: migrating journal schema 3 -> 6 in place: ~/.local/state/aiq/journal.sqlite3 (scope user, selected by --scope auto fallback outside any repository); forward-only, so AIQ installations older than schema 6 can no longer open this journal; pre-migration backup: ~/.local/state/aiq/backups/pre-migration-v3-to-v6-20260730T101112123456Z-9f2c.sqlite3
```

One line, once per migration; a journal already at the installed schema says
nothing. It names the resolved journal path, the stored and target schema
versions, and the pre-migration backup. A scope reached by fallback rather
than named says so, because the caller who did not choose this journal is the
caller most likely to be surprised that it is the one being changed; a scope
the caller named carries a plain `(scope repo)`.

The line is written after the backup exists and before anything is changed, so
its appearance proves the named backup is already on disk, and
[`recovery.md`](../recovery.md#an-unintended-migration) can be followed from
the line alone. It goes to stderr, never stdout, so a `--json` response is
still one clean document; and a stderr that is missing, closed, or unwritable
is ignored, because a diagnostic must never turn a working migration into a
failure.

This is human-readable output and therefore outside the [stability
boundary](#stability-boundary): its wording is not an interface and no
consumer may parse it.

The two installed hook paths deliberately do not announce, and keep migrating
silently:

| Path | Why silent |
|---|---|
| `UserPromptSubmit` capture | Documented silent on success and forbidden to write stdout at all, which the host injects into the prompt |
| `Stop` completion gate | Its block and stand-down outcomes are each exactly one stderr line, and both hosts feed that line back to the model; a second line would put an unrequested notice into a model's context |

Neither hook can choose a surprising journal the way a shell can: both resolve
scope from the host-supplied payload `cwd`, not from an ambient working
directory. A hook that migrates is an installation newer than the journal it
was installed alongside — the ordinary post-upgrade case — and whether a host
displays stderr from an exit-0 hook is host-dependent anyway.

### Alternatives considered

Recorded so a later session does not re-derive them. The incident that
prompted this: a checkout newer than the installed AIQ ran with its working
directory in a non-repository temporary directory, `auto` resolved to user
scope, and the user's real journal migrated from schema 3 to 6, locking out the
installed schema-4 CLI along with its capture hook and completion gate.

**Requiring acknowledgement for a migration crossing more than one version —
rejected.** The hop count is the wrong variable. Lockout is binary: a 3 → 4
migration locks out a schema-3 peer exactly as totally as 3 → 6 does, and AIQ
cannot see how many installations share a journal, so it cannot condition on
the risk that actually matters. Worse, a multi-version hop is the *ordinary*
upgrade: the table above shows a 0.1.0a2 installation may hold schema 2 while
the next release holds 6. Gating it would demand human interaction on the
common single-installation path, and the hook paths have no way to
acknowledge anything — capture would fail every prompt and the gate would
stand down until somebody ran a CLI by hand.

**Making `auto` refuse instead of falling back to user scope — rejected, on
design grounds rather than compatibility.** It is aimed at the wrong act. The
irreversible step is the migration, and the migration is equally silent under
an explicit `--scope user`, under `--scope repo` in an unexpected worktree or
submodule, and on the hook paths — which need "repo, else user" as a
first-class resolution and would have to keep it under another name. Refusing
closes one door into a room with several, while the announcement attaches to
the act itself and so covers all of them. The cost is also misplaced: `auto`
is the *default*, so refusing taxes every non-repository invocation by every
user, agent, and CI job, permanently, to guard against an operator error AIQ
already makes recoverable by design. Closing this at the resolver properly
would mean retiring the default rather than changing `auto`'s meaning — a
larger change than the incident warrants.

### Rollback

Rollback exists as a manual, last-resort operation, not a command. The alpha
CLI does not implement `journal restore`; see
[`recovery.md`](../recovery.md).

The pre-migration backup is written to a private `backups/` directory beside
the journal file — the sibling of the path `aiq journal path` reports, so
`<git-common-directory>/aiq/backups/` in repository scope and
`${XDG_STATE_HOME:-$HOME/.local/state}/aiq/backups/` in user scope. Its name
begins with `pre-migration-` and records the schema versions it spans; the
exact filename is not a stable interface. `aiq journal destroy --plan`
inventories these files, counted as `managed_backups`. AIQ retains the five
most recent pre-migration backups. That retention is fixed and independent of
`snapshot_keep` and `aiq journal snapshot --keep`, which govern only manual
snapshots.

Restoring one is a replacement, never a merge. It returns both the schema and
the contents to the instant before the migration, so every message, task,
application, claim, and lease recorded after that instant is discarded. Roll
back only when nothing worth keeping has been recorded since; otherwise
upgrade the lagging installation instead.

If rollback is still the right call, stop every AIQ process and disable every
installed hook for that scope first — the journal must have no writer — then
put the backup in place of `journal.sqlite3`, remove the stale `-wal` and
`-shm` sidecars, and verify with `aiq journal check` run from the older
installation. Retain the failed journal alongside its backups rather than
deleting it. Keep every pre-migration backup until the migrated journal has
passed `aiq journal check`.

## Stability boundary

The public boundary is:

- documented CLI commands, arguments, and exit codes;
- JSON response shapes;
- effects documents;
- capability descriptors; and
- integration manifests.

Python modules, functions, SQLite objects, events, migrations, locks, backup
filenames, human-readable output, and internal ordering machinery are not
public APIs. Persisted journals remain usable through the CLI, not through a
stable database schema.

Security and integrity corrections may reject an input previously accepted by
mistake. Such changes require a release note and a regression test.
