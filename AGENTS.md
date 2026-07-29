# Agent guidance

Practice thrift: never spend scarce resources twice—compute/flow, context/stock,
or user time/stalls. Practice determinism: codified answers repeat; re-derived
answers drift.

Use AIQ as durable local memory and a work queue. Capture requests and
material changes before relying on conversation context. Discover operations
with `aiq capability list`; load only the relevant contract with
`aiq capability show <id>`. Do not infer commands or database details.

At scheduling boundaries, check `aiq status` for runnable work. Before
declaring completion, record outcomes and verify no required runnable work
remains. Installed hooks enforce this at session stop. Keep AIQ runtime state
local and untracked; never commit journals, leases, exports, or snapshots.
File AIQ defects with `aiq report` (requires a configured `dev_report_repo`).

When reasoning becomes repeatable, encode it in the smallest deterministic
tool, test, check, or configuration, then point guidance there; build
repo-local tools with `tmt` where it exists, discovered via `tmt.json`.

When contributing to AIQ, follow `CONTRIBUTING.md`; load only documentation
relevant to the change.
