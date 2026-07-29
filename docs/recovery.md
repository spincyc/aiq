# Recovery

Start with the smallest read-only check that can identify the problem.

| Symptom | First action |
|---|---|
| Unsure which journal is active | `aiq journal path --json` |
| Suspected corruption or drift | `aiq journal check --json` |
| Worker stopped while holding work | Wait for lease expiry, then claim again |
| Worker can no longer finish | `aiq claim release CLAIM_ID` |
| Message needs clarification | `aiq inbox needs-input … --reason TEXT` |
| Message cannot be processed | `aiq inbox fail … --reason TEXT` |
| Before a risky local change | `aiq journal snapshot` |
| Need an inspectable external copy | `aiq journal export OUTPUT` |

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
