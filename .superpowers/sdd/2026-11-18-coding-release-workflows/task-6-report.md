# Task 6 report — npm and PyPI release adapters

## Result

Implemented `NpmPublishAdapter` and `PyPIPublishAdapter` with explicit official registry targets, workspace-confined artifacts, provider-specific credential profiles, bounded archive inspection, pre-upload digest checks, and provider read-back before reporting `PUBLISHED`.

The npm adapter requires an exact package name/version, official npm registry, visibility, and distribution tag. It rejects invalid unscoped names and semver-like tags, disables lifecycle scripts, compares SHA-512 SRI, reads the requested tag back, and confirms package access status.

The PyPI adapter requires an exact normalized project name, PEP 440 version, explicit PyPI or TestPyPI target, and validated wheel/sdist files. It uses `twine check` before upload, the selected fixed official upload URL, configured `__token__` credentials only, and exact filename/SHA-256 read-back. It never uses `--skip-existing` and does not fabricate Trusted Publishing/OIDC identity.

Both adapters block an existing version with a mismatched artifact, re-hash the selected workspace artifacts before upload, inspect remote state after timeout or command failure, and never treat a successful CLI exit by itself as proof of release. Provider output and operation results contain no credential values.

`packaging>=23,<27` is now a direct runtime dependency because the PyPI adapter uses `packaging.version`. The existing `packaging 26.3` resolution was retained in `uv.lock`, and the OLCR dependency and `requires-dist` entries were synchronized. `uv lock --offline` could not resolve uncached `fastapi` metadata for the Python 3.14/Windows split; no network lock regeneration was performed.

## Verification

Focused package and shared/provider regression suites:

```text
94 passed in 0.17s
```

The package adapter suite contributed 31 tests. Python byte-compilation and `git diff --check` passed. The independent read-only review found no P1/P2 issues. Tests use fake npm/Twine runners and fake API transports; no live npm, PyPI, or TestPyPI access or upload occurred.

## Official behavior checked

- [npm publish](https://docs.npmjs.com/cli/v12/commands/npm-publish/): exact package/version publication, immutable versions, SHA-512 SRI, access and tag options.
- [npm scopes](https://docs.npmjs.com/cli/v11/using-npm/scope/) and [package visibility](https://docs.npmjs.com/changing-package-visibility/): scoped package visibility behavior.
- [npm access status](https://docs.npmjs.com/cli/v11/commands/npm-access/): documented package access status read-back.
- [npm distribution tags](https://docs.npmjs.com/cli/v12/commands/npm-dist-tag/): tags interpreted as semantic version ranges are rejected.
- [npm provenance](https://docs.npmjs.com/generating-provenance-statements/): provenance requires an eligible configured CI/OIDC context.
- [PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/) and [publishing to PyPI](https://docs.pypi.org/publishing/publishing-to-pypi/): OIDC and API-token publishing requirements.
- [PyPI JSON API](https://docs.pypi.org/api/json/): exact release file names and SHA-256 digests for read-back.
