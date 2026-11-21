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
Set `f` to the wheel or the sdist name.
For a PyPI release, set `host` to `pypi.org`.

```bash
host=test.pypi.org
v=0.1.0rc1
f=scrimmage-$v-py3-none-any.whl
url=$(curl -sSf "https://$host/pypi/scrimmage/$v/json" | python3 -I -c 'import json,sys; print(next(u["url"] for u in json.load(sys.stdin)["urls"] if u["filename"]==sys.argv[1]))' "$f")
curl -sSf -o "$f" "$url"
curl -sSf -o "$f.provenance" "https://$host/integrity/scrimmage/$v/$f/provenance"
uvx pypi-attestations@0.0.31 verify pypi --repository https://github.com/ManishKumar97/scrimmage --provenance-file "$f.provenance" "$f"
```

The download builds and installs nothing, so no code from the index runs before the check.
It prints `OK:` and the file name when a workflow in this repository signed the file.
It fails when the file was changed after the upload, or when another repository signed it.
The index accepts uploads only from `release.yml`, through the trusted publisher set on each index.
The signature says who uploaded the file, not which commit it was built from.
The reproducible build covers that: a local build of the tagged commit gives the same bytes.
