# Privacy

AIQ is local software, but its journal may contain sensitive plaintext.

| Property | Alpha behavior |
|---|---|
| Network calls | None |
| Telemetry | None |
| Accounts or remote service | None |
| Raw message storage | Exact plaintext |
| Default retention | Indefinite |
| Selective deletion | Not supported |
| Complete export | Deterministic JSONL |
| Complete destruction | Two-phase, scope-fenced command |
| Journal permissions | Owner read/write (`0600`) |
| State-directory permissions | Owner only (`0700`) |

## Captured data

| Data | Examples |
|---|---|
| Message | Exact content, source, receive time |
| Source metadata | Session ID, turn ID, working directory |
| Derived work | Task titles, objectives, dependencies, priorities |
| History | Effects, revisions, claims, dispositions, integrity hashes |

Normal inbox listings omit content. `inbox claim` returns content because the
claimant must interpret it. `inbox list --include-content` exposes it
deliberately.

## Storage copies

Repository journals live below the Git common directory. User journals live
at `${XDG_STATE_HOME:-$HOME/.local/state}/aiq/journal.sqlite3`. Running
`aiq journal snapshot` creates another complete plaintext copy in the
journal's private `backups` directory.

The default snapshot command retains five snapshots and removes older
snapshots in that directory. Migration backups have their own bounded
retention. Copies made outside AIQ are not tracked or removed by AIQ.

### Integration backups

Integration install and uninstall keep exact pre-change copies of the complete
managed file (Codex `hooks.json`, Claude Code `settings.json`, or the selected
guidance file). Those files may contain unrelated hooks, settings, guidance, or
secrets. They are stored with mode `0600` below
`${XDG_STATE_HOME:-$HOME/.local/state}/aiq/integrations/<integration>/<target-id>/backups/`
and are retained indefinitely.

Journal destruction, package uninstall, and integration uninstall do not
remove these backups. AIQ has no integration-backup purge command yet. To
remove them manually, first uninstall the integration and verify the owned hook
is absent; then identify the exact `state_directory` reported by the
integration result and remove only that directory while no integration command
is running. Removing active integration state leaves an unmanaged hook and
prevents safe uninstall.

### Dev reports

`aiq report` copies data out of the reporting context: the summary and up to
16,000 characters of detail, which may quote prompts, paths, or other
sensitive text, are written into the configured AIQ development checkout's
repository-scope journal and retained indefinitely under that journal's
policies. Identical reports are deduplicated across reporting origins, and
the reporting repository's absolute working directory is recorded as the
message `cwd`. Destroying or exporting the reporting repository's own journal
never touches that copy; erase it through the development checkout's journal.

## Export

Export writes a new private `aiq-journal-jsonl` v1 file with media type
`application/x-ndjson`. It includes exact messages, events, task history,
applications, and claims with stored JSON decoded into semantic values. It
excludes physical scope identity, schema migrations, internal metadata,
allocator state, the ephemeral reader lease, and the holder locator recorded
on each claim — the host and session identifiers of both are live coordination
state rather than semantic history, so an export names no host and no session
id.

```sh
export_directory="${XDG_STATE_HOME:-$HOME/.local/state}/aiq-exports"
mkdir -p -m 700 "$export_directory"
chmod 700 "$export_directory"
aiq journal export "$export_directory/project-audit.jsonl"
```

The header declares `content: "full"` and `sensitive: true`; the final manifest
records counts and a content digest. An unchanged semantic journal produces
byte-identical output. Export refuses an existing path, uses mode `0600`, never
writes inside managed journal state, and is not managed after creation. Choose
a new output name for each export. JSONL is easy to inspect—and easy to commit
accidentally—so never create it inside a source worktree.

## Erasure

The event history is append-only, so individual messages cannot be erased
without invalidating the journal. Destruction therefore removes one complete
resolved journal, its SQLite sidecars, snapshots, and migration backups:

```sh
aiq journal destroy --plan
aiq journal destroy --confirm TOKEN
```

The plan is read-only and returns the exact inventory and a state-bound
confirmation token. Confirmation must use the same scope, working directory,
and configuration; any intervening state change invalidates the token.

Destroy does not remove external exports or copies. Remove integrations
separately if prompt capture must stop.

Destruction refuses unmanaged or unsafe entries instead of following them or
deleting unrelated files. An already absent journal is a successful
no-op. It retains the state parent and lifecycle lock for coordination. It
creates no backup, can be partial if an I/O failure interrupts deletion, and
is not a guarantee of physical secure erasure.
