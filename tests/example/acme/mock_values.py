"""Values a mock cannot hold in TOML, built by a function the test names with returns_from."""


def invoice_pdf() -> bytes:
    return b"%PDF-1.7 fixed test invoice"
