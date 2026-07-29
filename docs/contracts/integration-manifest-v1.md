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
| `managed_group` | Exact owned `UserPromptSubmit` hook group |
| `managed_group_sha256` | Canonical digest of that group |
| `config_sha256` | Digest of the target configuration after the operation |
| `created_file` | Whether AIQ created the target file |
| `created_containers` | JSON containers AIQ added |
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

## Safety semantics

`check` compares the manifest-owned group with current configuration.
`uninstall` removes that group only when ownership and content still match.
Unrelated groups and top-level fields are preserved.

A newly selected Python runtime or Git executable differs from the
manifest-owned group and is drift. Updating one requires explicit repair, which
replaces the owned hook and records the new paths and launcher identity. A
moved AIQ installation is repaired by running install from the replacement
installation. Changing the launcher identity alone does not change the owned
hook. If the recorded Python or Git executable becomes unavailable, hook
capture fails closed until the installation is repaired.

A reader must reject an unsupported future `v` without mutating either the
manifest or target. The current v1 manifest has an exact field set; missing or
unknown fields are treated as drift and fail without mutation.
