# Scrimmage

Scrimmage finds the silent failures in an AI agent, proves the fix, and stops them from coming back.

Docs: <https://manishkumar97.github.io/scrimmage/>

This repository is at an early stage.

## Install

Scrimmage is not released on PyPI yet, so install it from a clone.
It needs Python 3.11 or later.

```bash
git clone https://github.com/ManishKumar97/scrimmage
cd scrimmage
python -m venv .venv
. .venv/bin/activate
pip install ./packages/scrimmage
```

Then follow the [quickstart](docs/quickstart.md).
[Installing Scrimmage](docs/install.md) has uv, a wheel and the extras.

## Docs

The docs start at [docs/index.md](docs/index.md): install, a quickstart, concepts and every setting.
They are also a website, built with MkDocs: `uv run --group docs mkdocs build --strict` writes it to `site/`.

To report a security problem, see [SECURITY.md](SECURITY.md).

## License

Scrimmage is licensed under the Apache License 2.0; see [LICENSE](LICENSE).
