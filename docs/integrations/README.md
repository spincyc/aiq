# Integrations

An integration translates an external event into an AIQ message. It does not
give AIQ permission to execute the resulting work.

| Integration | Current alpha |
|---|---|
| [Generic](generic.md) | Manual text, stdin text, and canonical event JSON |
| [Claude Code](claude.md) | Reversible user-level `UserPromptSubmit` hook |
| [Codex](codex.md) | Reversible user-level `UserPromptSubmit` hook |
| [Guidance](guidance.md) | Reversible AIQ-owned block in an explicit local guidance file |
| Other agents | Use generic ingestion; add an adapter after observing a real contract |

## Boundary

```text
external event → adapter → AIQ message
```

Adapters should supply exact content, a stable source, absolute working
directory, and a stable idempotency identity when the host provides one.

## Agent bootstrap

```sh
aiq integration print agents
```

This prints the packaged [`AGENTS.md`](../../AGENTS.md) byte-for-byte. A
repository without guidance can save it as its root `AGENTS.md`. If guidance
already exists, inspect and integrate the terse directives without replacing
unrelated project instructions.

## Safe lifecycle

| Command | Behavior |
|---|---|
| `integration list` | Show available adapters |
| `integration plan` | Display the exact proposed changes |
| `integration print` | Emit fragments for manual management |
| `integration install` | Apply a minimal, recorded change |
| `integration check` | Detect missing files or configuration drift |
| `integration uninstall` | Remove only unchanged AIQ-owned material; the result names `integration_id` and `deleted_file` |

An installer must never replace an entire agent settings file. It must use
private backups, a versioned ownership manifest, atomic writes, and
compare-before-change behavior.

After an external AIQ upgrade, `aiq reconcile --user` reports whether
installed AIQ-owned hooks still match the current installation; with
`--apply` it repairs only manifest-owned material.
