# Versioning

Status: alpha contract.

AIQ versions its distribution, machine protocol, effects documents, capability
descriptors, and integration manifests independently. A storage schema version
is an implementation detail.

| Surface | Version location | Compatibility rule |
|---|---|---|
| Python distribution | `aiq --version` | Semantic Versioning |
| CLI JSON protocol | top-level `v` | A changed or removed field requires a new protocol version |
| Effects document | document `v` | Accepted syntax and meaning are immutable within a version |
| Capability descriptor | catalog `v` and capability `version` | A changed command contract increments the capability version |
| Integration manifest | manifest `v` | Readers reject unsupported future versions without mutation |
| Journal storage | internal metadata | AIQ migrates supported journals; callers must not inspect tables |

## Alpha policy

Before 1.0, a breaking package change increments the minor distribution
version. AIQ still does not silently reinterpret a versioned machine contract:
a breaking CLI response or effects change receives a new contract version.
Patch releases do not break documented contracts.

The following are compatible additions within CLI protocol v1:

- a new command or capability;
- a new optional object field;
- a new stable error code within an existing exit-code category; or
- a new enum value where the contract explicitly declares the enum extensible.

Consumers must ignore unknown object fields. They must not treat unknown enum
values as an existing value. Removing a field, changing its type or meaning,
changing array order, or changing an exit-code category is incompatible.

Effects inputs are strict rather than extensible. Unknown fields, operations,
and tuple members are rejected. Any accepted syntax or semantic change that is
not a defect correction requires a new effects version. AIQ may continue to
accept old effects versions after introducing a new one.

## Stability boundary

The public boundary is:

- documented CLI commands, arguments, and exit codes;
- JSON response shapes;
- effects documents;
- capability descriptors; and
- integration manifests.

Python modules, functions, SQLite objects, events, migrations, locks, backup
filenames, human-readable output, and internal ordering machinery are not
public APIs. Persisted journals remain usable through the CLI, not through a
stable database schema.

Security and integrity corrections may reject an input previously accepted by
mistake. Such changes require a release note and a regression test.
