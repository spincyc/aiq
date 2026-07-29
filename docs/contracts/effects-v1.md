# Effects document v1

One effects document interprets one claimed message. AIQ validates and commits
the whole document in one transaction.

The normative structure is
[`schemas/effects-v1.schema.json`](../../schemas/effects-v1.schema.json).

```json
{
  "v": 1,
  "expect": {
    "TASK-1": 3
  },
  "effects": [
    ["update", "TASK-1", {"priority": 10}]
  ]
}
```

| Field | Requirement |
|---|---|
| `v` | Integer `1` |
| `expect` | Current revision for every referenced existing task |
| `effects` | Ordered array of at most 64 operations |
| `reason` | Required only when `effects` is empty |

Unknown fields, duplicate JSON keys, invalid references, graph cycles, stale
revisions, and invalid transitions reject the complete document.

## Operations

| Operation | Shape |
|---|---|
| Create | `["create", "$alias", SPEC]` |
| Update | `["update", TASK, PATCH]` |
| Transition | `["transition", TASK, STATE, METADATA?]` |
| Add dependency | `["require", TASK, PREREQUISITE]` |
| Remove dependency | `["unrequire", TASK, PREREQUISITE]` |

### Create

| `SPEC` field | Value |
|---|---|
| `title` | Required nonempty string |
| `objective` | Optional string |
| `priority` | Optional integer; default `0` |
| `parent` | Optional task reference |
| `requires` | Optional array of task references |

A local alias starts with `$` and a lowercase letter, followed by at most 31
lowercase letters, digits, underscores, or hyphens. It is defined by a
preceding create and is visible only within this document.

### Update

`PATCH` accepts `title`, `objective`, `priority`, and `parent`. `objective` and
`parent` may be `null`. The patch must be nonempty and target a non-active,
non-terminal task.

### Transition

| Destination | Required metadata |
|---|---|
| `blocked` | `reason` |
| `canceled` | `reason` |
| `superseded` | `reason` and replacement task `by` |
| `done` | Current task `claim` |

`active` cannot be requested by an effect; it is derived from a queue claim.
Terminal tasks cannot transition again.

### Dependencies

Both task references must exist or be earlier local aliases. AIQ rejects
missing tasks, self-dependencies, duplicate changes, and dependency cycles.

## Retry and fencing

Existing references must appear in `expect`, including parents, dependencies,
and supersession replacements. A revision mismatch writes nothing.

Applying a canonically equivalent document again with the original consumed
message claim returns the stored result. A different document or claim is a
conflict.

Load the installed runtime contract before generating effects:

```sh
aiq capability show inbox.apply
```
