# Integration manifest v1

An integration manifest records what AIQ owns so later checks and uninstall
operations do not infer ownership from mutable user configuration.

| Property | Contract |
|---|---|
| Version | Top-level integer `v: 1` |
| Location | Private state below `${XDG_STATE_HOME:-$HOME/.local/state}/aiq/integrations/` |
| Permissions | Owner read/write (`0600`) |
| Mutation | Written atomically by integration commands |
| User editing | Unsupported; drift fails closed |

## Adapter fields

| Field | Meaning |
|---|---|
| `v` | Manifest contract version |
| `status` | `installed` or `uninstalled` |
| `integration` | Adapter name: `claude` or `codex` |
| `integration_id` | Stable marker embedded in the owned hook |
| `target` | Absolute managed configuration path (`hooks.json` for Codex, `settings.json` for Claude Code) |
| `launcher` | Lexical absolute AIQ console shim retained from install or repair as installation identity |
| `python_executable` | Absolute Python runtime used by the owned hook |
| `git_executable` | Lexical absolute Git executable embedded in the hook |
| `managed_group` | Ordered mapping of managed event name (`UserPromptSubmit`, `Stop`) to the exact owned hook group; a manifest from a single-event install may instead store one bare `UserPromptSubmit` group |
| `managed_group_sha256` | Canonical digest of the stored `managed_group` value |
| `config_sha256` | Digest of the target configuration after the operation |
| `created_file` | Whether AIQ created the target file |
| `created_containers` | JSON containers AIQ added, from the whitelist `hooks`, `UserPromptSubmit`, and `Stop` |
| `backups` | Private pre-change copies and their digests |

The target-specific state directory includes a digest of the absolute target
path, allowing distinct host configuration homes without sharing ownership
state.

The launcher is selected by explicit absolute `--launcher`, then the actual
absolute `aiq` console invocation, then `PATH`. Relative paths are invalid.
Symlinks are not resolved: the manifest retains the installer-owned absolute
shim identity. The hook does not contain or execute this path.

`python_executable` is `sys.executable` from the invoked AIQ installation at
install time. The owned command executes that absolute runtime as
`python_executable -I -m aiq`; it does not select Python through the hook
process's `PATH`.

The Git executable is selected by explicit absolute `--git-executable`, then
`git` on the install command's `PATH`. The selected executable is validated
and stored as a lexical absolute path without resolving symlinks. The owned
hook passes that path back to AIQ explicitly, so capture does not discover Git
from the host agent's process environment.

## Guidance fields

| Field | Meaning |
|---|---|
| `v` | Manifest contract version |
| `status` | `installed` or `uninstalled` |
| `integration` | Adapter name: `guidance` |
| `integration_id` | Stable identifier embedded in the block marker lines |
| `target` | Absolute managed guidance file path selected by explicit `--target` |
| `managed_block` | Exact owned marked block text |
| `managed_block_sha256` | Digest of that block |
| `separator` | Newline AIQ inserted before the appended block, if any |
| `config_sha256` | Digest of the target file after the operation |
| `created_file` | Whether AIQ created the target file |
| `backups` | Private pre-change copies and their digests |

## Safety semantics

`check` compares every manifest-owned group with current configuration.
`uninstall` removes those groups only when ownership and content still match.
Unrelated groups and top-level fields are preserved.

A newly selected Python runtime or Git executable differs from the
manifest-owned group and is drift. Updating one requires explicit repair, which
replaces the owned hook and records the new paths and launcher identity. A
moved AIQ installation is repaired by running install from the replacement
installation. Changing the launcher identity alone does not change the owned
hook. If the recorded Python or Git executable becomes unavailable, hook
capture fails closed until the installation is repaired.

A reader must reject an unsupported future `v` without mutating either the
manifest or target. The v1 manifest has a required field set; a missing or
invalid required field fails closed without mutation. Unknown additional
fields are preserved verbatim across operations so a newer AIQ can extend the
manifest without invalidating it for v1 readers.

`managed_group` is validated structurally — each owned group is an object
whose `hooks` list contains exactly one AIQ-marked command, mapping keys are
limited to the adapter's managed events, and the canonical digest of the
stored value matches `managed_group_sha256` — not by equality with the hook
template of the running AIQ version. A manifest written by an older AIQ
therefore remains valid after hook-template changes; the difference between
the owned groups and the currently desired definition surfaces as drift at
plan or check time and is resolved by explicit repair.

Both manifest shapes load under one `integration_id`: the bare single-group
shape written before multi-event support owns only the `UserPromptSubmit`
event, and the mapping shape owns one group per managed event. For an
installation recorded by a single-event manifest, the absent `Stop` group is
ordinary drift — `plan` and `check` report `drifted`, and `install --repair`
appends the missing `Stop` group, records any newly created `Stop` container,
and rewrites the manifest in the mapping shape. This is the supported upgrade
path from pre-gate installs; no manifest migration step exists or is needed.

If an AIQ-marked hook group exists in the target but no manifest records it
as installed (for example after an install interrupted between writing the
target and writing the manifest), plan and install report the state as
`unmanaged` and block. Running install with explicit repair adopts the marked
group: it replaces that group with the desired definition and writes a fresh
manifest recording `created_file` as `false` and `created_containers` as
empty, because AIQ cannot prove it created the file or containers.
