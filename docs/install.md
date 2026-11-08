# Install

Scrimmage needs Python 3.11 or newer, on Linux or macOS.

Install it in your **test environment only**, never in a production image.
It patches functions inside your agent's process, so it belongs where you test.

## From a clone (for now)

Scrimmage is not on PyPI yet.
The `scrim` package on PyPI is another project; here `scrim` is only a command name.
Build it from the repository, and install the result with pip looking only in that folder:

```bash
git clone https://github.com/ManishKumar97/faultline
cd faultline
uv build --all-packages --wheel -o dist
pip install --no-index --find-links dist scrimmage-core
```

## Check it worked

```bash
scrimmage --help
scrim --help
scrimmage-run python -c "print('ok')"
```

Two packages are installed:

| Package | Import name | What it is |
|---|---|---|
| `scrimmage-core` | `scrimmage_core` | The runner, the commands (`scrimmage`, its short name `scrim`, and `scrimmage-run`) and the code that runs inside your agent |
| `scrimmage` | `scrimmage` | A tiny package with no dependencies, for markers in your own code (later versions) |

Scrimmage depends on nothing outside Python's standard library, so installing it never changes the versions of the libraries your agent uses.
