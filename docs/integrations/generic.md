# Generic ingestion

Generic ingestion is the provider-neutral boundary for scripts and unsupported
agents.

| Input | Command | Status |
|---|---|---|
| Argument text | `aiq ingest --message TEXT` | Implemented |
| Raw UTF-8 stdin | `aiq ingest --stdin` | Implemented |
| Canonical event JSON file | `aiq ingest --event-json FILE` | Implemented |
| Canonical event JSON stdin | `aiq ingest --event-json -` | Implemented |

## Text

```sh
aiq ingest --source editor --message "Review the release"
printf '%s\n' "Review the release" | aiq ingest --source editor --stdin
```

Use `--idempotency-key KEY` when delivery may retry. Reusing a key with
identical content returns the existing message; reusing it with different
content fails.

Optional `--session-id` and `--turn-id` values form an automatic idempotency
key when both are present.

## Canonical event JSON

The strict v1 envelope is:

```json
{
  "v": 1,
  "source": "editor.bridge",
  "content": "Review the release",
  "idempotency_key": "editor:event-4",
  "session_id": "session-1",
  "turn_id": "turn-4",
  "cwd": "/absolute/path/to/project"
}
```

| Field | Requirement |
|---|---|
| `v` | Required integer `1` |
| `source` | Required; lowercase provider name, at most 64 UTF-8 bytes |
| `content` | Required nonempty string, at most 1 MiB UTF-8 |
| `idempotency_key` | Optional nonempty string, at most 512 UTF-8 bytes |
| `session_id` | Optional nonempty string, at most 256 UTF-8 bytes |
| `turn_id` | Optional nonempty string, at most 256 UTF-8 bytes |
| `cwd` | Optional absolute path, at most 4096 UTF-8 bytes |

Example:

```sh
printf '%s\n' \
  '{"v":1,"source":"editor.bridge","content":"Review the release","cwd":"/work/project"}' |
  aiq ingest --event-json - --quiet
```

Unknown fields, duplicate keys, invalid UTF-8, nonstandard JSON numbers, and
unsupported versions fail before journal mutation. An event `cwd` overrides
the command working directory for scope selection.

`--quiet` is appropriate for adapters because successful capture should not
alter the host's output. Errors still go to standard error and return nonzero.

## Adapter rules

| Rule | Purpose |
|---|---|
| Preserve multiline Unicode exactly | Keep source evidence trustworthy |
| Use a stable source name | Make provenance inspectable |
| Pass an absolute event working directory | Select the intended scope |
| Provide retry identity | Prevent duplicate messages |
| Invoke an absolute AIQ executable path | Avoid `PATH` substitution |
| Keep adapter output bounded | Protect local resources |

Host-specific adapters should translate their payload into this envelope
instead of adding provider fields to the generic contract.
