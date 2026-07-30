# Recovery

Start with the smallest read-only check that can identify the problem.

| Symptom | First action |
|---|---|
| Unsure which journal is active | `aiq journal path --json` |
| Suspected corruption or drift | `aiq journal check --json` |
| Worker stopped while holding work | Wait for lease expiry, then claim again |
| Worker can no longer finish | `aiq claim release CLAIM_ID` |
| Consuming fails with `reader_held` | `aiq reader status` |
| Message needs clarification | `aiq inbox needs-input … --reason TEXT` |
| Message cannot be processed | `aiq inbox fail … --reason TEXT` |
| Before a risky local change | `aiq journal snapshot` |
| Need an inspectable external copy | `aiq journal export OUTPUT` |
| An installation suddenly reports `schema_incompatible` | [An unintended migration](#an-unintended-migration) |

## Integrity

`journal check` verifies SQLite integrity, foreign keys, scope metadata, schema
objects, content hashes, append-only event relationships, tasks, revisions,
applications, and claims.

Do not edit the SQLite database to repair a failed check. Preserve the exact
error, resolved journal path, and affected files for diagnosis.

## Snapshots

```sh
aiq journal check
aiq journal snapshot --keep 5
```

A snapshot is a verified, standalone SQLite backup with private permissions.
Snapshot creation refuses an invalid journal and prunes only snapshots beyond
the requested retention.

## Claims

Claims are leases, not durable locks. An expired claim is fenced from further
application or completion. A later claim operation records expiry and makes
eligible work available again.

Release a live claim when stopping intentionally:

```sh
aiq claim release CLAIM_ID
```

Repeating a successful release returns the stored release result.

## Stale reader leases

One scope has one reader role, so a crashed consumer can leave it held. Find
the holder first:

```sh
aiq reader status
```

A `stale` lease is unexpired but its recorded holder is provably gone, so the
next consume takes it over with nothing to do. An `expired` lease is likewise
already recoverable. A `held` lease from a session that has genuinely
gone away is reclaimed one of three ways, in order of preference: have the
holder run `aiq reader release`; wait for `expires_at`, bounded by
`reader_lease_seconds`; or, when the holder derived its identity on this host
and its session is provably gone, the next consume takes over immediately.

Releasing takes proof of holding, so naming the holder's identity is refused
rather than honored: `aiq reader release --reader THEIR_ID` reports
`reader_held` and exit 4. Breaking a live lease is a deliberate, named act:

```sh
aiq reader release --force
```

Reach for it only when the three ways above cannot work — most often a lease
held by a host-identified session that is gone for good, which is never proved
dead and so would otherwise wait out a `reader_lease_seconds` of up to a day.
It frees the role and records no release for anybody, so no session's
completion gate is stood down by it. It is a last resort because two sessions
draining one journal at once is exactly what the role exists to prevent.

Losing the role revokes nothing. Claims the old holder still holds recover on
their own lease schedule, and the old holder can still apply, park, fail,
release, and complete the work it already claimed.

## Export is not restore

`journal export` produces complete deterministic JSONL for inspection and
portable archival. It is not accepted as a journal snapshot and AIQ does not
automatically manage or erase it.

## Restore limitation

The alpha CLI does not yet implement `journal restore`. Do not overwrite a live
journal with a snapshot while AIQ processes may be running.

A reusable restore operation must validate the source snapshot, take a
pre-restore snapshot, replace the journal atomically, invalidate old leases,
and verify the result. Until that exists, retain both the failed journal and
its snapshots and use a reviewed, environment-specific recovery procedure.

## Migrations

AIQ validates the existing schema before migration, creates a private
pre-migration backup, and rejects schemas newer than it understands. Keep that
backup until the migrated journal passes `journal check`.

Every journal-opening CLI command announces a migration it is about to run,
on stderr, before changing anything:

```text
aiq: migrating journal schema 3 -> 6 in place: ~/.local/state/aiq/journal.sqlite3 (scope user, selected by --scope auto fallback outside any repository); forward-only, so AIQ installations older than schema 6 can no longer open this journal; pre-migration backup: ~/.local/state/aiq/backups/pre-migration-v3-to-v6-20260730T101112123456Z-9f2c.sqlite3
```

Read that line before doing anything else. It names the journal being
changed, so a journal you did not intend to touch is visible at the moment
it matters, and it names the backup — which already exists by the time the
line is printed. The installed capture and completion-gate hooks migrate
silently instead; [`versioning.md`](contracts/versioning.md#announcing-a-migration)
explains why.

## An unintended migration

The failure this section exists for: a newer AIQ opened a journal that an
older, still-installed AIQ was using, and migrated it forward. Migration is
one-way, so the older installation is now locked out of its own journal.

### Telling that it happened

The older installation fails every journal-opening command with
[`schema_incompatible`](contracts/errors.md) and exit 5. Two quieter symptoms
usually arrive first, because neither stops anybody:

- prompts stop being journaled — the older installation's `UserPromptSubmit`
  capture hook exits 1 with one `AIQ prompt capture failed` stderr line; and
- completion stops being enforced — its `Stop` gate fails open, exiting 0 with
  one `AIQ completion gate skipped` stderr line.

Confirm the diagnosis from the journal itself, using an AIQ new enough to open
it. The last migration row records the hop and names the backup taken for it:

```sh
aiq journal path --json
sqlite3 "$(aiq journal path)" \
  'SELECT from_version, to_version, migrated_at, backup_name
     FROM schema_migrations ORDER BY migration_id DESC LIMIT 1;'
```

A `migrated_at` you did not expect, or a `from_version` matching the schema
your installed AIQ still speaks, is the confirmation. Reading the table this
way is a diagnostic, not an interface: nothing outside AIQ may depend on it.

### Deciding whether to roll back

Rolling back is a replacement, not a merge: everything recorded since the
migration is discarded. Prefer upgrading the lagging installation. Roll back
only when nothing worth keeping has been recorded since the migration — which
is most plausible when the migration was moments ago and the journal has had
no writer since.

### Finding and verifying the backup

The pre-migration backup sits in a private `backups/` directory beside the
journal — `<git-common-directory>/aiq/backups/` in repository scope,
`${XDG_STATE_HOME:-$HOME/.local/state}/aiq/backups/` in user scope. Its name
begins with `pre-migration-` and records the versions it spans. AIQ keeps the
five most recent.

Verify it before trusting it. Confirm it is intact, confirm it holds the
schema you expect, and confirm its contents match the live journal's history
up to the migration:

```sh
backup=~/.local/state/aiq/backups/pre-migration-v3-to-v6-…sqlite3
live="$(aiq journal path)"

sqlite3 "$backup" 'PRAGMA integrity_check;'          # expect: ok
sqlite3 "$backup" 'PRAGMA foreign_key_check;'        # expect: no output
sqlite3 "$backup" \
  "SELECT value FROM journal_metadata WHERE key = 'schema_version';"
```

The schema must be the version the locked-out installation speaks. Then diff
the contents against the live journal, so a restore's cost is known rather
than assumed. Attach both files read-only and ask what the live journal has
that the backup does not — that set is exactly what a restore would discard:

```sh
sqlite3 "file:$backup?mode=ro" "
  ATTACH DATABASE 'file:$live?mode=ro' AS live;
  SELECT 'messages', COUNT(*) FROM live.messages
   WHERE message_id NOT IN (SELECT message_id FROM main.messages)
  UNION ALL
  SELECT 'events', COUNT(*) FROM live.events
   WHERE event_id NOT IN (SELECT event_id FROM main.events);
"
```

Both counts zero means the migration recorded nothing of its own and the
restore is exact — the case worth acting on without further thought. Any
nonzero count is work the restore destroys; list it before deciding:

```sh
sqlite3 "file:$backup?mode=ro" "
  ATTACH DATABASE 'file:$live?mode=ro' AS live;
  SELECT received_at, source, substr(content, 1, 60) FROM live.messages
   WHERE message_id NOT IN (SELECT message_id FROM main.messages)
   ORDER BY received_at;
"
```

`aiq journal export OUTPUT` also produces deterministic JSONL of the *live*
journal for archival before a restore. Take one: it costs nothing and it is
the only inspectable copy of whatever the restore is about to discard.

### Restoring

There is no `journal restore` command; this is a manual replacement. The
journal must have no writer while it happens.

1. Stop every AIQ process reaching this scope, and disable every installed
   hook for it (`aiq integration uninstall claude --user`, and the same for
   any other adapter), so no capture or gate reopens the journal mid-restore.
2. Move the migrated journal aside — do not delete it:
   `mv "$live" "$live.migrated"`, and move `"$live-wal"` and `"$live-shm"`
   aside with it. Stale sidecars belonging to the migrated file will corrupt a
   restored journal.
3. Copy the verified backup into place: `cp "$backup" "$live"`, then
   `chmod 600 "$live"`. Copy, never move: the backup stays where it is.
4. Verify from the installation that was locked out — that is the check that
   matters, because reopening from a newer AIQ would migrate it straight back
   out again: `aiq journal check`.
5. Reinstall the hooks, and confirm capture works before relying on it:
   `aiq doctor`.

Retain `"$live.migrated"` and every pre-migration backup until the restored
journal has passed `aiq journal check` and normal work has resumed.

### Preventing a repeat

The migration happened because some installation newer than the journal opened
it. Keep every AIQ that reaches one journal on one version, and name the scope
you mean — `--scope repo` or `--scope user` — whenever the working directory
is not obviously inside the repository you intend, because `--scope auto`
outside a repository silently means user scope, which is the journal every
installed hook is bound to.
`aiq journal path` answers which journal a command would use, before it runs.
See [`versioning.md`](contracts/versioning.md#journal-schema-and-shared-installations).
