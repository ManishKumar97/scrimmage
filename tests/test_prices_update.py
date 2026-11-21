"""scrimmage prices update: fetch over HTTPS, check the schema, print the diff, then write."""

import http.server
import ssl
import stat
import threading
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

import pytest

from scrimmage import cli, prices, prices_update
from scrimmage.cert import make_certificates

NEWER = (
    resources.files("scrimmage")
    .joinpath("prices.toml")
    .read_text(encoding="utf-8")
    .replace('as_of = "2026-11-19"', 'as_of = "2026-12-01"')
    .replace("input = 2.00\n", "input = 2.25\n", 1)  # gpt-4.1
    .replace(
        '[models."gpt-5-nano"]',
        '[models."gpt-9"]\ninput = 1.0\noutput = 2.0\n\n[models."gpt-5-nano"]',
    )
)
BAD = NEWER.replace("input = 2.25", 'input = "two"')


class Server:
    def __init__(self, certs: Path) -> None:
        self.body = NEWER.encode()
        self.status = 200
        self.asked = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.asked += 1
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.http = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certs / "server.pem", certs / "server.key")
        self.http.socket = context.wrap_socket(self.http.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.http.server_address[1]}/prices.toml"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(5)


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    files = make_certificates(tmp_path / "certs", ips=("127.0.0.1",))
    monkeypatch.setattr(prices_update, "CAFILE", str(files.ca))
    server = Server(tmp_path / "certs")
    monkeypatch.setattr(prices_update, "URL", server.url)
    yield server
    server.stop()


def project(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "scrimmage.toml").write_text("")
    return folder


@pytest.mark.design("prices.update")
def test_update_prints_the_diff_and_writes_the_team_file(
    served: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(tmp_path)
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 0
    out = capsys.readouterr().out
    assert "added gpt-9: input 1, output 2" in out
    assert "changed gpt-4.1: input 2 to 2.25" in out
    assert "removed" not in out
    assert "the new file is as of 2026-12-01" in out
    runs = folder / "scrimmage-runs"
    assert (runs / "prices.toml").read_text() == NEWER
    assert (runs / ".gitignore").read_text() == "*\n"
    assert stat.S_IMODE(runs.stat().st_mode) == 0o700
    assert prices.team(folder) is not None
    assert [p.name for p in runs.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.design("prices.update")
def test_a_second_update_compares_with_the_team_file(
    served: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(tmp_path)
    cli.main(["prices", "update", str(folder / "scrimmage.toml")])
    capsys.readouterr()
    served.body = NEWER.replace("input = 2.25", "input = 2.50").encode()
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 0
    out = capsys.readouterr().out
    assert "compared with scrimmage-runs/prices.toml" in out
    assert "changed gpt-4.1: input 2.25 to 2.5" in out
    assert "gpt-9" not in out


@pytest.mark.design("prices.update")
def test_a_file_that_fails_its_schema_writes_nothing_and_exits_2(
    served: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(tmp_path)
    served.body = BAD.encode()
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 2
    err = capsys.readouterr().err
    assert "models gpt-4.1 input: expected dollars per million tokens" in err
    assert "nothing was written" in err
    assert not (folder / "scrimmage-runs").exists()
    assert served.asked == 1


@pytest.mark.design("prices.update")
def test_a_bad_file_does_not_replace_a_good_team_file(
    served: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(tmp_path)
    cli.main(["prices", "update", str(folder / "scrimmage.toml")])
    served.body = b"as_of = 5"
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 2
    assert (folder / "scrimmage-runs" / "prices.toml").read_text() == NEWER


@pytest.mark.design("prices.update")
def test_an_unreachable_server_writes_nothing_and_exits_3(
    served: Server,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = project(tmp_path)
    served.stop()
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 3
    assert "nothing was written" in capsys.readouterr().err
    assert not (folder / "scrimmage-runs").exists()


@pytest.mark.design("prices.update")
def test_a_server_error_exits_3_and_a_certificate_we_do_not_trust_exits_3(
    served: Server,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = project(tmp_path)
    served.status = 500
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 3
    served.status = 200
    monkeypatch.setattr(
        prices_update, "CAFILE", None
    )  # the system's authorities do not know the test's
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 3
    assert not (folder / "scrimmage-runs").exists()


@pytest.mark.design("prices.update")
def test_only_an_https_address_is_fetched(tmp_path: Path) -> None:
    with pytest.raises(prices_update.FetchError, match="not an https address"):
        prices_update.fetch("http://127.0.0.1:1/prices.toml")
    with pytest.raises(prices_update.FetchError, match="not an https address"):
        prices_update.fetch("file:///etc/passwd")


@pytest.mark.design("prices.file_never_free")
@pytest.mark.parametrize(
    "free",
    ['[models."gpt-4.1"]\ninput = 0\noutput = 0\n', '[models."gpt-4.1"]\ninput = 1\noutput = 0\n'],
)
def test_a_fetched_file_that_makes_a_model_free_writes_nothing_and_exits_2(
    served: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str], free: str
) -> None:
    folder = project(tmp_path)
    served.body = ('as_of = "2026-12-01"\nsource = "x"\n' + free).encode()
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 2
    assert "is not above 0" in capsys.readouterr().err
    assert not (folder / "scrimmage-runs").exists()
