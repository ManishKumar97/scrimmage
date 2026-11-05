# Install

Faultline needs Python 3.11 or newer, on Linux or macOS.

Install it in your **test environment only**, never in a production image.
It patches functions inside your agent's process, so it belongs where you test.

## From a clone (for now)

Faultline is not on PyPI yet, and another project already uses the name `faultline` there.
Install from the repository, and tell pip not to look at PyPI:

```bash
git clone https://github.com/ManishKumar97/faultline
cd faultline
uv build --all-packages --wheel -o dist
pip install --no-index --find-links dist faultline
```

`--no-index` matters: without it, pip may install the unrelated PyPI package of the same name.

## Check it worked

```bash
faultline --help
faultline-run python -c "print('ok')"
```

Two packages are installed:

| Package | Import name | What it is |
|---|---|---|
| `faultline` | `faultline_sdk` | The runner, the commands and the code that runs inside your agent |
| `faultline-marker` | `faultline` | A tiny package with no dependencies, for markers in your own code (later versions) |

Faultline depends on nothing outside Python's standard library, so installing it never changes the versions of the libraries your agent uses.
