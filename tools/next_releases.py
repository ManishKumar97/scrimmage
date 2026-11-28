"""Name what each gated library will ship next, as one matrix leg per library and source.

    python tools/next_releases.py   # the matrix, as JSON, on stdout

Two sources per library:

- Its default branch on GitHub, pinned to the commit it has now. That is what ships next.
- Its newest pre-release on PyPI, unless it is stale: superseded by a stable release, or
  older than STALE_DAYS with a stable release uploaded after it (a 1.0 development build
  left from before the current 0.29).

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
from datetime import UTC, datetime

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


# A pre-release older than this, with a stable release uploaded after it, is left behind.
STALE_DAYS = 60


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


def next_prerelease(found: list[Release], now: datetime) -> Release | None:
    """The newest pre-release that is still ahead of what is stable, if there is one.

    A pre-release is stale when a stable release has superseded it: one at or above the
    pre-release's own version. Otherwise it counts when it was uploaded after the newest
    stable release (a patch of an older line, uploaded later, does not drop it) or within
    the last STALE_DAYS days. A 1.0 development build left from before the stable 0.29
    is neither, so it is skipped.
    """
    stable = [r for r in found if not r.version.is_prerelease]
    newest = max(stable, key=lambda r: r.uploaded, default=None)
    ahead = [
        r
        for r in found
        if r.version.is_prerelease
        and not any(s.version >= r.version for s in stable)
        and (
            newest is None or r.uploaded > newest.uploaded or (now - r.uploaded).days <= STALE_DAYS
        )
    ]
    return max(ahead, key=lambda r: r.version, default=None)


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
    pre = next_prerelease(releases(name), datetime.now(UTC))
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
