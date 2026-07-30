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

A `held` lease with an `expires_at` in the past is already recoverable and the
next consume takes it over. A live lease from a session that has genuinely
gone away is reclaimed one of three ways, in order of preference: have the
holder run `aiq reader release`; wait for `expires_at`, bounded by
`reader_lease_seconds`; or, when the holder derived its identity on this host
and its session is provably gone, the next consume takes over immediately.
There is deliberately no force or steal — a live lease is refused rather than
broken, because breaking one risks two sessions draining a journal at once.

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
