# Install

Scrimmage runs on Python 3.11, 3.12, 3.13 and 3.14.
The test suite runs on all four versions, on Linux.
It is tested on Linux only. macOS is expected to work, but it is not in CI.

Install it in your **test environment only**, never in a production image.
It patches functions inside your agent's process while a test runs, so it belongs where you test.

## The two packages

| Package | Import name | What it is |
|---|---|---|
| `scrimmage-core` | `scrimmage_core` | The runner, the commands (`scrimmage`, its short name `scrim`, and `scrimmage-run`), and the code that runs inside your agent during a test |
| `scrimmage` | `scrimmage` | A small package with no dependencies, for marking tools in your own code. It does nothing unless `scrimmage-core` is installed. In this version it holds no markers yet |

`scrimmage-core` depends on `scrimmage` and on nothing else outside Python's standard library.
So installing it never changes the versions of the libraries your agent uses.

The marker package is installed now, though it holds no markers yet, because it reserves the import name `scrimmage`.
Later versions put markers there, and code that imports it keeps working, with or without `scrimmage` installed.

## From a clone

Scrimmage is not on PyPI yet, so install it from the repository.
The `scrim` package on PyPI is another project; here `scrim` is only a command name.
Each command below installs both packages; name the marker first, so it is found in the clone.

```bash
git clone https://github.com/ManishKumar97/faultline
cd faultline
```

### With pip

pip is Python's standard package installer.

```bash
python -m venv .venv
. .venv/bin/activate
pip install ./packages/scrimmage ./packages/scrimmage-core
```

### With uv

uv is a fast Python package and project manager from Astral, used in place of pip and venv.

```bash
uv venv
. .venv/bin/activate
uv pip install ./packages/scrimmage ./packages/scrimmage-core
```

To use a Python version other than uv's default, add it to the first command, such as `uv venv -p 3.11`.

### As wheel files

A wheel is Python's built package format.
Build both wheels once, then install them where the clone is not available, such as a CI image:

```bash
uv build --all-packages --wheel -o dist
uv pip install --no-index --find-links dist scrimmage-core
```

`--no-index` makes the installer look only in `dist`, so nothing of the same name is taken from PyPI.
With pip, the second command is `pip install --no-index --find-links dist scrimmage-core`.

## Check it worked

```bash
scrimmage --help
scrim --help
scrimmage-run python -c "print('ok')"
python -c "import scrimmage_core, scrimmage"
```

The first two print the same commands, the third prints `ok`, and the last prints nothing.
`scrimmage-run` with no test running changes nothing in the program it starts.
See [Commands](cli.md).

Your agent also needs its own libraries in the same environment, such as `openai`.
Then try the [Quickstart](quickstart.md).
