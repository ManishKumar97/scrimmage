# Releasing Scrimmage

This note is for maintainers.
A release is a tag push.
The workflow in `.github/workflows/release.yml` does the rest.
No API token is stored anywhere.
Uploads use PyPI trusted publishing.

## Steps

1. Bump `version` in `packages/scrimmage/pyproject.toml` in a pull request, and merge it.
2. An admin tags the merge commit on main with `v<version>`, for example `v0.1.0rc1`.
   The tag ruleset allows only admins to push a `v*` tag.
3. The build job fails unless the tagged commit is on main.
4. The build job fails unless the tag is exactly `v` plus the built version.
5. The `check` job runs `twine check --strict` on the built files, in a job that cannot change them.
6. The `testpypi` job publishes to TestPyPI with no approval.
7. For a final version such as `v0.1.0`, the `pypi` job then waits for approval of the `pypi` environment.
   It publishes to PyPI after the approval.
8. A release candidate such as `v0.1.0rc1` stops after TestPyPI.
   The `pypi` job is skipped.

## The version rule

The version must be `X.Y.Z` or `X.Y.ZrcN`, with plain digits.
Any other spelling fails the build job before anything is uploaded.
That includes `0.1.0c1`, `0.1.0a1`, `0.1.0b1`, `0.1.0.dev0` and `0.1.0-rc1`.
The build job sets `prerelease` from the version, and the `pypi` job runs only when it is `false`.

## Why a release candidate comes first

A version number can never be reused on PyPI or on TestPyPI, even after a delete.
A failed first upload burns the number.
So the first run of a new workflow uses an rc tag, and a retry uses the next rc.
The final version is tagged only after an rc has passed.

## Verifying the attestation

The upload action signs each file with Sigstore, and the index serves the signature as a provenance file.
To verify a file, download it and its provenance, then check both against this repository.
For TestPyPI, use `test.pypi.org` in the provenance URL; for PyPI, use `pypi.org`.

```bash
f=scrimmage-0.1.0rc1-py3-none-any.whl
uvx pip download --no-deps --only-binary :all: --index-url https://test.pypi.org/simple/ scrimmage==0.1.0rc1
curl -s -o "$f.provenance" "https://test.pypi.org/integrity/scrimmage/0.1.0rc1/$f/provenance"
uvx pypi-attestations verify pypi --repository https://github.com/ManishKumar97/scrimmage --provenance-file "$f.provenance" "$f"
```

It prints `OK:` and the file name when the file was built and uploaded by `release.yml` in this repository.
It fails when the file was changed after the upload, or when another repository signed it.
