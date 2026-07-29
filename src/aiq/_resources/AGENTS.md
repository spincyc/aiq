# Agent guidance

Practice thrift: never spend scarce resources twice—compute/flow, context/stock,
or user time/stalls. Practice determinism: codified answers repeat; re-derived
answers drift.

Use AIQ as durable local memory and a work queue. Capture new requests and
material changes before relying on conversation context. Discover operations
with `aiq capability list`; inspect only the relevant contract with
`aiq capability show <id>`. Do not infer commands or database details.

At scheduling boundaries, inspect AIQ for runnable work. Before declaring
completion, record outcomes and verify no required runnable work remains. Keep
AIQ runtime state local and untracked; never commit journals, leases, exports,
or snapshots.

When reasoning becomes repeatable, encode it in the smallest deterministic
tool, test, check, or configuration, then point guidance to that artifact.

When contributing to AIQ, follow `CONTRIBUTING.md` and load only the
documentation relevant to the change.
