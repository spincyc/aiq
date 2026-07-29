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
| `snapshot_keep` | `1`–`10000` | `5` | yes | no |
| `output` | `human`, `json` | `human` | yes | no |

Repository configuration can set only bounded lease duration. It cannot choose
scope, identity, output, or retention. `snapshot_keep` controls snapshot
retention only; it never removes messages or task history.

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
| `AIQ_SNAPSHOT_KEEP` | `snapshot_keep` |
| `AIQ_OUTPUT` | `output` |

Integer variables must contain unsigned decimal digits.

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
