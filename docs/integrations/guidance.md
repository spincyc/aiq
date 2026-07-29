# Guidance integration

The guidance integration manages one AIQ-owned bootstrap block inside a local
agent guidance file that the user selects explicitly.

| Property | Alpha status |
|---|---|
| Managed content | One marked block containing the packaged `AGENTS.md` bootstrap |
| Target selection | Explicit absolute `--target`; no location is ever inferred |
| Markers | `<!-- aiq-guidance-v1:begin id=aiq-workqueue.guidance.v1 -->` and the matching `end` line |
| Lifecycle | `plan`, `install`, `check`, and `uninstall` |
| Unrelated content | Preserved byte-for-byte |
| Whole-file replacement | Prohibited |
| Network or telemetry | None |

## Install

```sh
aiq integration plan guidance --target /absolute/path/AGENTS.md
aiq integration install guidance --target /absolute/path/AGENTS.md
aiq integration check guidance --target /absolute/path/AGENTS.md
```

`plan` and `check` are read-only. Install appends the owned marked block,
creates the target file only when it is absent, and records a private manifest
and exact-byte backups below
`${XDG_STATE_HOME:-$HOME/.local/state}/aiq/integrations/guidance/<target-id>/`.
Package installation never mutates guidance; only these explicit commands do.
AIQ never infers a Codex home, repository root, or dotfiles location for this
target.

## Lifecycle guarantees

| Guarantee | Result |
|---|---|
| Preview | `plan` performs no mutation and returns a `plan_token` |
| Minimal diff | Only the marked block is added, replaced, or removed |
| Repeatability | Repeated install converges |
| Drift safety | An edited or unmanaged block fails closed |
| Reversibility | Uninstall restores the pre-install bytes exactly |

The normative marker, ownership, drift, and repair rules live in the
[CLI contract](../contracts/cli-v1.md#integrations); this page is a
walkthrough. `check` reports `trust: "not_applicable"` because the owned
block contains no hook command to review.

## Repair

An edited owned block, or packaged bootstrap content that changed after an
AIQ upgrade, is drift. Review a new plan, then repair explicitly:

```sh
aiq integration plan guidance --target /absolute/path/AGENTS.md --repair
aiq integration install guidance --target /absolute/path/AGENTS.md --repair
aiq integration check guidance --target /absolute/path/AGENTS.md
```

Repair replaces only the owned marked block and preserves every unrelated
byte.

## Uninstall

```sh
aiq integration uninstall guidance --target /absolute/path/AGENTS.md
```

It removes only the unchanged owned block, restores surrounding bytes exactly,
and deletes the file only when AIQ created it and nothing else remains.
Private backups are retained; see
[Privacy](../privacy.md#integration-backups).
