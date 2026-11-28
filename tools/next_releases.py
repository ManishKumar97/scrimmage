"""Name what each gated library will ship next, as one matrix leg per library and source.

    python tools/next_releases.py   # the matrix, as JSON, on stdout

Two sources per library:

- Its default branch on GitHub, pinned to the commit it has now. That is what ships next.
- Its newest pre-release on PyPI, only when that was uploaded after the newest stable
  release. An older pre-release (a 1.0 development build from before the current stable
  line) is stale and is skipped.

Each leg installs only its own library's next version over the lock, and the job runs the
gate tests on it. A leg that cannot install or import fails by itself.
Standard library only, besides `packaging`, which pytest brings.
One line per leg goes to stderr, so the log names each library and the exact version or
commit it ran.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime

from packaging.version import InvalidVersion, Version

# PyPI name: (repository on GitHub, folder of its pyproject.toml or "" for the root,
# the other projects of the same repository it needs at the same commit)
LIBRARIES: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {
    "openai": ("openai/openai-python", "", ()),
    "anthropic": ("anthropics/anthropic-sdk-python", "", ()),
    "httpx": ("encode/httpx", "", ()),
    "httpx2": ("pydantic/httpx2", "src/httpx2", (("httpcore2", "src/httpcore2"),)),
    "langchain-core": ("langchain-ai/langchain", "libs/core", ()),
    "langchain-openai": (
        "langchain-ai/langchain",
        "libs/partners/openai",
        (("langchain-core", "libs/core"),),
    ),
    "langgraph": ("langchain-ai/langgraph", "libs/langgraph", ()),
    "mcp": ("modelcontextprotocol/python-sdk", "", ()),
}


class Release:
    def __init__(self, version: Version, uploaded: datetime) -> None:
        self.version = version
        self.uploaded = uploaded


def releases(name: str) -> list[Release]:
    """The versions PyPI has for a project that still have files, none of them yanked."""
    url = f"https://pypi.org/pypi/{name}/json"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = json.load(response)
    found: list[Release] = []
    for text, files in data["releases"].items():
        if not files or any(f.get("yanked") for f in files):
            continue
        try:
            version = Version(text)
        except InvalidVersion:
            continue
        uploaded = min(datetime.fromisoformat(f["upload_time_iso_8601"]) for f in files)
        found.append(Release(version, uploaded))
    return sorted(found, key=lambda r: r.version)


def next_prerelease(found: list[Release]) -> Release | None:
    """The newest pre-release that is newer than the newest stable release, by version and
    by upload time, if there is one."""
    stable = [r for r in found if not r.version.is_prerelease]
    newest = max(stable, key=lambda r: r.version, default=None)
    ahead = [
        r
        for r in found
        if r.version.is_prerelease
        and (newest is None or (r.version > newest.version and r.uploaded > newest.uploaded))
    ]
    return ahead[-1] if ahead else None


def head(repo: str) -> tuple[str, str]:
    """The default branch of a repository and the commit it is at now."""
    url = f"https://github.com/{repo}"
    out = subprocess.run(
        ["git", "ls-remote", "--symref", url, "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    branch = ""
    commit = ""
    for line in out.splitlines():
        if line.startswith("ref: "):
            branch = line.split()[1].removeprefix("refs/heads/")
        elif line.endswith("\tHEAD"):
            commit = line.split("\t")[0]
    if not branch or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError(f"could not read the default branch of {repo}: {out!r}")
    return branch, commit


def git_requirement(name: str, repo: str, commit: str, folder: str) -> str:
    suffix = f"#subdirectory={folder}" if folder else ""
    return f"{name} @ git+https://github.com/{repo}@{commit}{suffix}"


def legs(name: str) -> list[dict[str, str]]:
    """The matrix legs for one library: its default branch, and a fresh pre-release."""
    repo, folder, companions = LIBRARIES[name]
    branch, commit = head(repo)
    wanted = [git_requirement(name, repo, commit, folder)]
    wanted += [git_requirement(other, repo, commit, sub) for other, sub in companions]
    out = [
        {
            "name": f"{name}, default branch {branch}",
            "library": name,
            "source": f"branch {branch} of {repo} at commit {commit}",
            "requirements": "\n".join(wanted),
        }
    ]
    pre = next_prerelease(releases(name))
    if pre is not None:
        out.append(
            {
                "name": f"{name}, pre-release {pre.version}",
                "library": name,
                "source": f"pre-release {pre.version} from PyPI, uploaded {pre.uploaded:%Y-%m-%d}",
                "requirements": f"{name}=={pre.version}",
            }
        )
    return out


def main() -> int:
    matrix: list[dict[str, str]] = []
    for name in LIBRARIES:
        matrix += legs(name)
    for leg in matrix:
        print(f"{leg['name']}: {leg['source']}", file=sys.stderr)
    print(json.dumps(matrix))
    return 0


if __name__ == "__main__":
    sys.exit(main())
