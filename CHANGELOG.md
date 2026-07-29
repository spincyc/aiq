# Changelog

Notable user-visible changes are recorded here.

This project uses [Semantic Versioning](https://semver.org/). Public
compatibility covers documented CLI behavior, exit codes, versioned JSON and
effects documents, capability contracts, and integration manifests.

## [Unreleased]

### Added

- Initial local SQLite journal and durable message inbox.
- Deterministic task effects, dependency ordering, leases, and integrity
  checks.
- Compact capability discovery for agents and humans.
- Packaged, context-bounded `AGENTS.md` bootstrap guidance.
- Source-first Python packaging under the `aiq-workqueue` distribution name.
- Installer-neutral pipx and virtual-environment workflows.
- Codex hooks bound to recorded absolute Python and Git executables.
- Codex lifecycle capability descriptors v2 with explicit Git selection.
- Fail-closed automatic scope selection when Git is unavailable or fails.

[Unreleased]: https://github.com/spincyc/aiq/commits/main
