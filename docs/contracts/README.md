# Public contracts

AIQ's distribution is alpha. Its machine-facing contracts are explicitly
versioned and are not silently reinterpreted within a version.

| Surface | Alpha contract |
|---|---|
| [CLI JSON v1](cli-v1.md) | Versioned success envelopes and projections |
| [Errors](errors.md) | Stable codes and exit categories |
| [Effects document](effects-v1.md) | Versioned as `v: 1` |
| Capability IDs and contracts | Authoritative discovery for implemented operations |
| [Versioning](versioning.md) | Compatibility policy |
| Python modules | Internal; no compatibility promise |
| SQLite schema | Internal; access only through AIQ |
| [Integration manifest v1](integration-manifest-v1.md) | Versioned ownership record |

Pin an alpha distribution release when automating AIQ. See
[Versioning](versioning.md) for compatible additions and breaking changes.

## Discovery

```sh
aiq capability list
aiq capability show inbox.apply
aiq --version
```

`capability show` is authoritative when this documentation and an installed
alpha differ.

## Error behavior

Human errors are single-line escaped text on standard error and are not
versioned. With `--json`, errors use:

```json
{"code":"invalid_document","error":"message","status":"error","v":1}
```

Automation should branch on the stable code and documented exit category, not
on human wording.
