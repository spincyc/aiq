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

## Codex fields

| Field | Meaning |
|---|---|
| `v` | Manifest contract version |
| `status` | `installed` or `uninstalled` |
| `integration` | Adapter name: `codex` |
| `integration_id` | Stable marker embedded in the owned hook |
| `target` | Absolute managed `hooks.json` path |
| `launcher` | Absolute AIQ executable recorded at install |
| `managed_group` | Exact owned `UserPromptSubmit` hook group |
| `managed_group_sha256` | Canonical digest of that group |
| `config_sha256` | Digest of the target configuration after the operation |
| `created_file` | Whether AIQ created the target file |
| `created_containers` | JSON containers AIQ added |
| `backups` | Private pre-change copies and their digests |

The target-specific state directory includes a digest of the absolute target
path, allowing distinct Codex homes without sharing ownership state.

## Safety semantics

`check` compares the manifest-owned group with current configuration.
`uninstall` removes that group only when ownership and content still match.
Unrelated groups and top-level fields are preserved.

A reader must reject an unsupported future `v` without mutating either the
manifest or target. The current v1 manifest has an exact field set; missing or
unknown fields are treated as drift and fail without mutation.
