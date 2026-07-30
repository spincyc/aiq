# Configuration

AIQ resolves each setting independently through a strict precedence chain:

```text
CLI → AIQ environment → repository .aiq.toml → XDG user config → defaults
```

Both TOML files are optional, flat, and must declare `version = 1`.

| Source | Location or form |
|---|---|
| CLI | Command option |
| Environment | `AIQ_*` variable |
| Repository | Exact Git worktree root: `.aiq.toml` |
| User | `$XDG_CONFIG_HOME/aiq/config.toml` |
| User fallback | `$HOME/.config/aiq/config.toml` |

`XDG_CONFIG_HOME` and `HOME` must be absolute when used for discovery.

## Keys

| Key | Values | Default | User | Repository |
|---|---|---:|:---:|:---:|
| `scope` | `auto`, `repo`, `user` | `auto` | yes | no |
| `owner` | Nonempty local identity | OS user | yes | no |
| `lease_seconds` | `1`–`86400` | `900` | yes | yes |
| `reader` | Nonempty session identity | The [session identity](#session-identity) | yes | no |
| `reader_lease_seconds` | `60`–`86400` | `1800` | yes | yes |
| `snapshot_keep` | `1`–`10000` | `5` | yes | no |
| `output` | `human`, `json` | `human` | yes | no |
| `dev_report_repo` | Absolute path | `None` | yes | no |

Repository configuration can set only bounded lease durations. It cannot choose
scope, identity, output, or retention. `snapshot_keep` controls snapshot
retention only; it never removes messages or task history. `dev_report_repo`
names the local AIQ development checkout that receives `aiq report` bug
reports; it is deliberately excluded from repository configuration so a cloned
repository cannot redirect reports. The additional `agent-root` value visible
in the CLI `--scope` choices is an internal, unstable hook and is not a
configurable scope.

`owner` and `reader` are different identities on purpose. `owner` labels who
holds a message or task claim and defaults to the OS user, so two concurrent
sessions of one person share it. `reader` names the session allowed to drain
the scope's queue; its default is derived, and [Session
identity](#session-identity) below says how. Set `reader_lease_seconds` at or
above `lease_seconds` so the reader role never expires before the items it
holds. Exporting one shared `AIQ_READER` is the documented way to let several
cooperating workers drain a single journal on purpose — at the cost of the
per-session answers described under [Reader identity and session
identity](#reader-identity-and-session-identity), including the ability to end
a bounded run with `reader release`.

Example user configuration:

```toml
version = 1
scope = "auto"
owner = "local-worker"
lease_seconds = 900
snapshot_keep = 5
output = "human"
```

Example repository policy:

```toml
version = 1
lease_seconds = 1200
```

## Environment

| Variable | Key |
|---|---|
| `AIQ_SCOPE` | `scope` |
| `AIQ_OWNER` | `owner` |
| `AIQ_LEASE_SECONDS` | `lease_seconds` |
| `AIQ_READER` | `reader` |
| `AIQ_READER_LEASE_SECONDS` | `reader_lease_seconds` |
| `AIQ_SNAPSHOT_KEEP` | `snapshot_keep` |
| `AIQ_OUTPUT` | `output` |
| `AIQ_DEV_REPORT_REPO` | `dev_report_repo` |

Integer variables must contain unsigned decimal digits.

`AIQ_SESSION_ID` is listed separately below: it names the session rather than
setting a configuration key.

## Session identity

Several answers depend on AIQ recognizing that two commands, or a command and
a host hook, belong to the same session: who may drain the queue, whose claims
are outstanding, and whether a completion gate should stand down.

### Reader identity and session identity

These are two different values, and only one of them is configurable.

The **reader identity** is the name the reader role is held under. It is what
`reader status` prints and what `--reader` accepts, and AIQ takes it from the
first of:

| Precedence | Source | Notes |
|---:|---|---|
| 1 | `--reader`, `AIQ_READER`, or `reader` in a user config file | An explicit identity, used verbatim. It may deliberately name a group rather than a session |
| 2 | The session identity below | The default, and the only form that names one session |

The **session identity** is how AIQ tells one session from another when it
compares a recorded lease or claim against the caller. It is *never* taken
from `reader`, and comes from the first of:

| Precedence | Source | Notes |
|---:|---|---|
| 1 | `AIQ_SESSION_ID` | The generic override. Any host, wrapper, or launcher can export it without AIQ knowing which host it is |
| 2 | The `session_id` in a hook payload | Authoritative for the session being gated, and inherited by nothing |
| 3 | The host's own variable — today `CLAUDE_CODE_SESSION_ID` | Claude Code exports it into every command it runs, and puts the same value in the `session_id` of that session's hook payloads |
| 4 | The host name plus the POSIX session id | Last resort |

**Setting `reader` explicitly gives up the per-session answers.** A configured
identity may name any session on any host — that is the point of a shared
fan-out name — so a lease taken under one records no session locator at all,
and nothing can later prove such a lease belongs to the caller in front of it.
Dispatch still works: the role is held, other readers are still excluded, and
`reader release` still hands it back. What does not work is every answer that
needs to identify *a session*: `reader.live` and `reader.released_by_self` both
read false, and so **a bounded run cannot end on its own release** — the
completion gate keeps blocking. `reader release` says so on stderr and reports
`declared` false. Configure `reader` for cooperating fan-out workers that drain
until empty; leave it unset for a session that stops on a bound.

The last resort is only a session identity where one POSIX session spans many
commands. That is true of a terminal, where every command and every hook runs
as a child of the one session, and false of a host that runs each command in a
session of its own — which agent hosts commonly do, and Claude Code does. On
such a host the process that took a lease or a claim has exited before
anything else asks about it, so nothing later can recognize its own earlier
work: `aiq reader release` matches no lease and reports `not_held`, the
completion gate keeps blocking, and each command competes with the last for
the reader role.

**So on a host AIQ does not already know, export `AIQ_SESSION_ID`.** One value
per session, stable for its whole life, distinct between concurrent sessions —
a UUID the wrapper generates at startup is ideal. Nothing else needs
configuring.

A `Stop` hook is compared against the `session_id` in its own payload, which
outranks anything in the hook process's environment except `AIQ_SESSION_ID`.
That is what lets the gate and the commands of one session agree even though
neither can see the other's environment.

Changing a session's identity — starting to export `AIQ_SESSION_ID` where
nothing was exported before, or moving between hosts — makes the leases and
claims already recorded read as a stranger's. Nothing breaks and nothing is
lost: a lease recorded that way is reclaimed by the next command once the
POSIX session it named is gone, or when it expires; a claim recorded that way
counts toward `claims.active` but not `claims.active_this_session`, so a
completion gate still names it, and it clears when it expires or is settled.
See [`contracts/cli-v1.md`](contracts/cli-v1.md).

## Scope

| Scope | Resolution |
|---|---|
| `auto` | Repository scope inside Git; otherwise user scope |
| `repo` | Git common directory for the working directory |
| `user` | `${XDG_STATE_HOME:-$HOME/.local/state}/aiq/journal.sqlite3` |

Use `aiq journal path --json` before capture when the selected scope is
uncertain.

## Validation

| Rule | Result |
|---|---|
| Reject unknown keys and invalid types | Prevent silent policy drift |
| Treat repository config as untrusted | A cloned repository must not gain execution authority |
| Allow only bounded declarative values | Configuration must not execute commands |
| Keep secrets out of AIQ config | Journals and manifests are not secret stores |
| Resolve each value with its source | Make precedence inspectable |

Repository config must be a regular, non-symlink file. User config may be a
symlink to a regular file. Invalid configuration fails closed instead of being
silently ignored. Repository discovery ignores ambient `GIT_*` overrides so
another repository cannot redirect the working directory's policy.

## Inspect configuration

| Command | Result |
|---|---|
| `aiq config show` | Print effective values |
| `aiq config show --sources` | Include the winning source for each value |
| `aiq config check` | Validate all discovered layers |

Configuration inspection is read-only. It does not initialize or mutate a
journal. Add `--no-repo-config` to skip repository policy explicitly, including
when diagnosing an invalid cloned configuration.
