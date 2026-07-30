# Contributing

AIQ favors small, deterministic changes that conserve compute, context, and
user time.

## Before changing code

- Search existing issues and pull requests.
- Open an issue before changing a public CLI, exit code, JSON shape, effects
  document, capability contract, or integration manifest.
- Never include journals, snapshots, prompts, credentials, personal paths, or
  other private data in an issue, test, commit, or pull request.
- Report vulnerabilities through the process in
  [SECURITY.md](SECURITY.md), not through a public issue.

## Development

AIQ requires Python 3.11–3.14 and SQLite 3.37 or newer. Create an isolated
environment and install the checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --editable .
```

`make install-packages` detects and loads a supported package fragment:

| `AIQ_PLATFORM` | Fragment | Installer |
|---|---|---|
| `arch` | [`make/platforms/arch.mk`](make/platforms/arch.mk) | `pacman` through `sudo` |
| `macos` | [`make/platforms/macos.mk`](make/platforms/macos.mk) | Existing Homebrew |

Override detection when necessary:

```sh
make AIQ_PLATFORM=arch install-packages
```

An unsupported selection exits without installing. Add a platform fragment or
install equivalent tools with the native package manager.

Use `make verify` as the fast default check during development. Before
submitting a change, run the full CI-parity check:

```sh
make ci
```

`make ci` runs exactly what the CI policy leg runs: `verify` (which starts
with `tools/sanity-check`), `public-audit`, `release-check`, `build`, and
`tools/acceptance-install`. A passing `make ci` means the policy leg of CI
will pass on the same inputs.

`make release-check` asserts that `pyproject.toml`, `_SOURCE_VERSION` in
`src/aiq/__init__.py`, and the newest version section of `CHANGELOG.md` all
name one version, and that the changelog's sections, headings, and link
references are well formed. When cutting a release, name the tag as well —
`make release-check TAG=v0.3.0a1` — to check it against the same version.
CI runs that form automatically on a `v*` tag push.

## Pull requests

- Keep the smallest coherent diff.
- Add tests for changed behavior and public contracts.
- Update documentation and `CHANGELOG.md` for user-visible changes.
- Preserve append-only journal integrity and migration paths.
- Explain verification performed and any checks that could not run.

By contributing, you agree that your contribution is licensed under the
Apache License 2.0.
