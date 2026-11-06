# Install

Derail needs Python 3.11 or newer, on Linux or macOS.

Install it in your **test environment only**, never in a production image.
It patches functions inside your agent's process, so it belongs where you test.

## From a clone (for now)

Derail is not on PyPI yet.
Build it from the repository, and install the result with pip looking only in that folder:

```bash
git clone https://github.com/ManishKumar97/derail
cd derail
uv build --all-packages --wheel -o dist
pip install --no-index --find-links dist derail
```

## Check it worked

```bash
derail --help
derail-run python -c "print('ok')"
```

Two packages are installed:

| Package | Import name | What it is |
|---|---|---|
| `derail` | `derail_sdk` | The runner, the commands and the code that runs inside your agent |
| `derail-marker` | `derail` | A tiny package with no dependencies, for markers in your own code (later versions) |

Derail depends on nothing outside Python's standard library, so installing it never changes the versions of the libraries your agent uses.
