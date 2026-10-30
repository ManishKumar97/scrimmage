from pathlib import Path

import pytest
import yaml

from faultline_lab.incidents.catalog import CATALOG_PATH, load_catalog

ROW = {
    "name": "cart_service_failure",
    "flag": "cartFailure",
    "variant": "on",
    "service": "cart",
    "source": "aiopslab/orchestrator/problems/cart_service_failure/cart_service_failure.py",
}
CONTROL = {"name": "no_op", "source": "aiopslab/orchestrator/problems/no_op/no_op.py"}


def write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "incidents.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_the_shipped_catalog_has_eleven_incidents_and_the_control():
    catalog = load_catalog()

    assert len(catalog.incidents) == 11
    assert catalog.control.name == "no_op"
    assert CATALOG_PATH.name == "incidents.yaml"


def test_answer_key_matches_the_aiopslab_labels():
    catalog = load_catalog()

    assert catalog.get("payment_service_unreachable").service == "checkout"
    assert catalog.get("image_slow_load").service == "frontend"
    assert catalog.get("payment_service_failure").variant == "100%"
    assert catalog.get("image_slow_load").variant == "10sec"
    assert catalog.get("kafka_queue_problems").variant == "on"


def test_variants_stay_strings():
    # YAML turns a bare on into True. A bool here would write the wrong flag value.
    assert all(isinstance(i.variant, str) for i in load_catalog().incidents)


def test_sources_exist_in_aiopslab_when_it_is_checked_out():
    root = Path.home() / "AIOpsLab"
    if not root.exists():
        pytest.skip("AIOpsLab is not checked out")
    for incident in load_catalog().incidents:
        assert (root / incident.source).is_file(), incident.name


def test_a_valid_minimal_file_loads(tmp_path):
    catalog = load_catalog(write(tmp_path, {"incidents": [ROW], "control": CONTROL}))

    assert catalog.get("cart_service_failure").service == "cart"


def test_a_duplicate_flag_is_rejected(tmp_path):
    other = {**ROW, "name": "other"}
    path = write(tmp_path, {"incidents": [ROW, other], "control": CONTROL})

    with pytest.raises(ValueError, match="duplicate incident flag"):
        load_catalog(path)


def test_a_duplicate_name_is_rejected(tmp_path):
    other = {**ROW, "flag": "otherFlag"}
    path = write(tmp_path, {"incidents": [ROW, other], "control": CONTROL})

    with pytest.raises(ValueError, match="duplicate incident name"):
        load_catalog(path)


@pytest.mark.parametrize(
    "bad",
    [
        {"service": "Cart Service"},
        {"service": ""},
        {"variant": ""},
        {"source": "somewhere/else.txt"},
        {"unknown": 1},
    ],
)
def test_a_bad_row_is_rejected(tmp_path, bad):
    path = write(tmp_path, {"incidents": [{**ROW, **bad}], "control": CONTROL})

    with pytest.raises(ValueError, match="invalid"):
        load_catalog(path)


def test_missing_control_and_empty_list_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="invalid"):
        load_catalog(write(tmp_path, {"incidents": [ROW]}))
    with pytest.raises(ValueError, match="invalid"):
        load_catalog(write(tmp_path, {"incidents": [], "control": CONTROL}))


def test_a_missing_file_names_the_path(tmp_path):
    with pytest.raises(ValueError, match="nope.yaml"):
        load_catalog(tmp_path / "nope.yaml")
