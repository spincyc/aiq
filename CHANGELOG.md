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

[Unreleased]: https://github.com/spincyc/aiq/commits/main
