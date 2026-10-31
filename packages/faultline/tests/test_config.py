import pytest

from faultline import cli
from faultline.config import Attributes, Config, ConfigError, load_config, parse_config

MAPPING = """
[attributes]
question = "my.question"
answer = "my.answer"
stop_reason = "my.stop"
"""


def test_the_defaults_map_nothing_and_set_the_limits():
    config = Config()

    assert config.attributes == Attributes(None, None, None)
    assert config.detect.tool_timeout_seconds == 30.0
    assert config.checks.repeated_call_limit == 3


def test_a_config_file_maps_attributes_and_sets_limits():
    text = (
        MAPPING
        + "[detect]\ntool_timeout_seconds = 10\n[checks]\nrepeated_call_limit = 5\n"
    )

    config = parse_config(text)

    assert config.attributes == Attributes("my.question", "my.answer", "my.stop")
    assert config.detect.tool_timeout_seconds == 10.0
    assert isinstance(config.detect.tool_timeout_seconds, float)
    assert config.checks.repeated_call_limit == 5


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("[attributes]\nquestoin = 'x'\n", "attributes.questoin"),
        ("[detect]\ntimeout = 3\n", "detect.timeout"),
        ("[checks]\nlimit = 3\n", "checks.limit"),
        ("[atributes]\n", "atributes"),
        ("[detect]\ntool_timeout_seconds = 'slow'\n", "detect.tool_timeout_seconds"),
        ("[checks]\nrepeated_call_limit = true\n", "checks.repeated_call_limit"),
        ("[attributes]\nquestion = 3\n", "attributes.question"),
    ],
)
def test_a_bad_key_or_value_is_an_error_that_names_it(text, named):
    with pytest.raises(ConfigError, match=named):
        parse_config(text)


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("[detect]\ntool_timeout_seconds = 0\n", "detect.tool_timeout_seconds"),
        ("[detect]\ntool_timeout_seconds = -1.5\n", "detect.tool_timeout_seconds"),
        ("[checks]\nrepeated_call_limit = 0\n", "checks.repeated_call_limit"),
    ],
)
def test_a_limit_that_cannot_work_is_an_error_that_names_it(text, named):
    with pytest.raises(ConfigError, match=named):
        parse_config(text)


def test_the_smallest_allowed_limits_are_accepted():
    text = "[checks]\nrepeated_call_limit = 1\n[detect]\ntool_timeout_seconds = 0.1\n"

    config = parse_config(text)

    assert config.checks.repeated_call_limit == 1
    assert config.detect.tool_timeout_seconds == 0.1


def test_a_file_that_is_not_toml_is_an_error():
    with pytest.raises(ConfigError, match="not valid TOML"):
        parse_config("[attributes")


def test_a_given_path_that_is_missing_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_path / "none.toml")


def test_the_default_file_is_used_only_when_it_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config() == Config()

    (tmp_path / "faultline.toml").write_text(MAPPING)

    assert load_config().attributes.question == "my.question"


def test_find_reports_a_bad_config_and_exits_2(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[attributes]\nquestoin = 'x'\n")

    assert cli.main(["find", "--config", str(bad), str(bad)]) == 2

    captured = capsys.readouterr()
    assert "attributes.questoin" in captured.err
    assert captured.out == ""
