# Install

Scrimmage runs on Python 3.11, 3.12, 3.13 and 3.14.
The test suite runs on all four versions, on Linux.
It is tested on Linux only. macOS is expected to work, but it is not in CI.

Install it in your **test environment only**, never in a production image.
It patches functions inside your agent's process while a test runs, so it belongs where you test.

## The package

| Package | Import name | What it is |
|---|---|---|
| `scrimmage` | `scrimmage_core` | The runner, the commands (`scrimmage`, `scrimmage-run` and `scrimmage-mcp`), and the code that runs inside your agent during a test |

It depends on nothing outside Python's standard library.
So installing it never changes the versions of the libraries your agent uses.
You never type the import name: you write `scrimmage.toml` and run the commands.

It has two extras.
`scrimmage[mcp]` adds the Scrimmage MCP server, `scrimmage-mcp`, and the `mcp` library it needs; see [Driving Scrimmage from a coding agent](mcp-server.md).
`scrimmage[cert]` adds the `cryptography` library that `scrimmage cert` needs.

## From a clone

Scrimmage is not released yet, so install it from the repository.
The name `scrimmage` on PyPI holds only an empty placeholder, version 0.0.0.
The package in the repository is version 0.1.0.dev0, a development release.
pip and uv skip a development release when a final one is found, so a plain `pip install scrimmage` takes the placeholder.
Install from the folder, or from a wheel with `--no-index`, as below.

```bash
git clone https://github.com/ManishKumar97/scrimmage
cd scrimmage
```

### With pip

pip is Python's standard package installer.

```bash
python -m venv .venv
. .venv/bin/activate
pip install ./packages/scrimmage
```

### With uv

uv is a fast Python package and project manager from Astral, used in place of pip and venv.

```bash
uv venv
. .venv/bin/activate
uv pip install ./packages/scrimmage
```

To use a Python version other than uv's default, add it to the first command, such as `uv venv -p 3.11`.

### As wheel files

A wheel is Python's built package format.
Build the wheel once, then install it where the clone is not available, such as a CI image:

```bash
uv build --package scrimmage --wheel -o dist
uv pip install --no-index --find-links dist scrimmage
```

`--no-index` makes the installer look only in `dist`, so nothing of the same name is taken from PyPI.
With pip, the second command is `pip install --no-index --find-links dist scrimmage`.

## Check it worked

```bash
scrimmage --help
scrimmage-run python -c "print('ok')"
python -c "import scrimmage_core"
```

The first prints the commands, the second prints `ok`, and the last prints nothing.
`scrimmage-run` with no test running changes nothing in the program it starts.
See [Commands](cli.md).

Your agent also needs its own libraries in the same environment, such as `openai`.
Then try the [Quickstart](quickstart.md).
