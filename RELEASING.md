# Releasing Scrimmage

This note is for maintainers.
A release is a tag push.
The workflow in `.github/workflows/release.yml` does the rest.
No API token is stored anywhere.
Uploads use PyPI trusted publishing.

## Steps

1. Bump `version` in `packages/scrimmage/pyproject.toml` in a pull request, and merge it.
2. An admin pushes the tag `v<version>`, for example `v0.1.0rc1`. The tag ruleset allows only admins to push a `v*` tag.
3. The build job checks that the tag is exactly `v` plus the built version. A mismatch fails the run before anything is uploaded.
4. The `testpypi` job publishes to TestPyPI with no approval.
5. For a final version such as `v0.1.0`, the `pypi` job then waits for approval of the `pypi` environment. It publishes to PyPI after the approval.
6. A tag that contains `rc` stops after TestPyPI. The `pypi` job is skipped.

## Why a release candidate comes first

A version number can never be reused on PyPI or on TestPyPI, even after a delete.
A failed first upload burns the number.
So the first run of a new workflow uses an rc tag, and a retry uses the next rc.
The final version is tagged only after an rc has passed.

## Verifying the attestation

The upload action signs each file with Sigstore.
The command to verify it is to be confirmed on the first rc.
