"""Name what each gated library will ship next, as install requirements.

    python tools/next_releases.py > next-requirements.txt

For each library: its newest pre-release on PyPI when that is newer than its newest stable
release, as `name==version`. Where there is none, the library's default branch on GitHub,
pinned to the commit it has now, as `name @ git+https://...@<commit>`.
A default branch can break for reasons that never ship, so a pre-release is preferred.
Standard library only, besides `packaging`, which pytest brings.

The requirements go to stdout. One line per library, saying what was chosen and why, goes
to stderr, so the job's log names each library and the exact version or commit it ran.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

from packaging.version import InvalidVersion, Version

# PyPI name: (repository on GitHub, folder of its pyproject.toml, "" for the root)
LIBRARIES: dict[str, tuple[str, str]] = {
    "openai": ("openai/openai-python", ""),
    "anthropic": ("anthropics/anthropic-sdk-python", ""),
    "httpx": ("encode/httpx", ""),
    "httpx2": ("pydantic/httpx2", "src/httpx2"),
    # httpx2's own dependency, from the same repository: the branch's httpx2 may need its
    # unreleased httpcore2.
    "httpcore2": ("pydantic/httpx2", "src/httpcore2"),
    "langchain-core": ("langchain-ai/langchain", "libs/core"),
    "langchain-openai": ("langchain-ai/langchain", "libs/partners/openai"),
    "langgraph": ("langchain-ai/langgraph", "libs/langgraph"),
    "mcp": ("modelcontextprotocol/python-sdk", ""),
}


def releases(name: str) -> list[Version]:
    """The versions PyPI has for a project that still have files, none of them yanked."""
    url = f"https://pypi.org/pypi/{name}/json"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = json.load(response)
    found: list[Version] = []
    for text, files in data["releases"].items():
        if not files or any(f.get("yanked") for f in files):
            continue
        try:
            found.append(Version(text))
        except InvalidVersion:
            continue
    return sorted(found)


def next_prerelease(versions: list[Version]) -> Version | None:
    """The newest pre-release that is newer than every stable release, if there is one."""
    stable = [v for v in versions if not v.is_prerelease]
    ahead = [v for v in versions if v.is_prerelease and (not stable or v > stable[-1])]
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
    if not branch or len(commit) != 40:
        raise RuntimeError(f"could not read the default branch of {repo}: {out!r}")
    return branch, commit


def requirement(name: str, repo: str, folder: str) -> tuple[str, str]:
    """(the requirement line, what the log says about it)."""
    pre = next_prerelease(releases(name))
    if pre is not None:
        return f"{name}=={pre}", f"{name}: pre-release {pre} from PyPI"
    branch, commit = head(repo)
    suffix = f"#subdirectory={folder}" if folder else ""
    line = f"{name} @ git+https://github.com/{repo}@{commit}{suffix}"
    return line, f"{name}: no pre-release, default branch {branch} of {repo} at commit {commit}"


def main() -> int:
    lines: list[str] = []
    for name, (repo, folder) in LIBRARIES.items():
        line, said = requirement(name, repo, folder)
        lines.append(line)
        print(said, file=sys.stderr)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
