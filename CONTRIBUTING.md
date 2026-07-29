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

On Arch Linux, `make install-packages` installs the declared development
tools. On other supported systems, provide equivalent `git`, `gitleaks`,
`make`, `pipx`, and Python build commands.

Run the complete local checks before submitting a change:

```sh
make verify
make public-audit
make build
```

## Pull requests

- Keep the smallest coherent diff.
- Add tests for changed behavior and public contracts.
- Update documentation and `CHANGELOG.md` for user-visible changes.
- Preserve append-only journal integrity and migration paths.
- Explain verification performed and any checks that could not run.

By contributing, you agree that your contribution is licensed under the
Apache License 2.0.
