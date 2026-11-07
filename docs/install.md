# Install

Derail needs Python 3.11 or newer, on Linux or macOS.

Install it in your **test environment only**, never in a production image.
It patches functions inside your agent's process, so it belongs where you test.

## From a clone (for now)

Derail is not on PyPI yet.
Build it from the repository, and install the result with pip looking only in that folder:

```bash
git clone https://github.com/ManishKumar97/faultline
cd faultline
uv build --all-packages --wheel -o dist
pip install --no-index --find-links dist scrimmage-core
```

## Check it worked

```bash
derail --help
derail-run python -c "print('ok')"
```

Two packages are installed:

| Package | Import name | What it is |
|---|---|---|
| `scrimmage-core` | `scrimmage_core` | The runner, the commands and the code that runs inside your agent |
| `scrimmage` | `derail` | A tiny package with no dependencies, for markers in your own code (later versions) |

Derail depends on nothing outside Python's standard library, so installing it never changes the versions of the libraries your agent uses.
