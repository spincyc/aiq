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
| CLI JSON protocol | top-level `v` | A changed or removed field requires a new protocol version |
| Effects document | document `v` | Accepted syntax and meaning are immutable within a version |
| Capability descriptor | catalog `v` and capability `version` | A changed command contract increments the capability version |
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
changing array order, or changing an exit-code category is incompatible.

Effects inputs are strict rather than extensible. Unknown fields, operations,
and tuple members are rejected. Any accepted syntax or semantic change that is
not a defect correction requires a new effects version. AIQ may continue to
accept old effects versions after introducing a new one.

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
stale installation degrades quietly. Version 0.2.0a1 moved storage to
schema 4 and therefore triggers all of it. The remedy is to upgrade every AIQ
installation that reaches the journal and then re-bind the AIQ-owned material
with `aiq reconcile --user --apply`, which reports each adapter it could not
repair. Nothing already recorded is lost by the migration itself; what is lost
is whatever an unupgraded hook failed to capture in the meantime.

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
