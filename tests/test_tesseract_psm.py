import json

import pytest

from apps.ocr.pipeline import default_engine_plans
from apps.ocr.tesseract_psm import MEASURED_MODES, MEASUREMENTS_PATH, default_psm


def test_every_source_type_compares_all_candidate_modes() -> None:
    payload = json.loads(MEASUREMENTS_PATH.read_text(encoding="utf-8"))
    assert payload["metric"] == "exact_expected_field_accuracy"
    assert all(
        {int(mode) for mode in scores} == MEASURED_MODES
        for scores in payload["source_types"].values()
    )


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        ("bank_transaction_list", 6),
        ("credit_card_statement", 6),
        ("bank_transaction_detail", 11),
        ("credit_card_payment", 11),
        ("unrecognized", 6),
    ],
)
def test_defaults_are_derived_from_checked_in_measurements(source_type: str, expected: int) -> None:
    assert default_psm(source_type) == expected


def test_default_plan_records_measured_mode_and_allows_per_run_override() -> None:
    measured = default_engine_plans(source_type="card_transaction_detail")[1]
    overridden = default_engine_plans(source_type="card_transaction_detail", tesseract_psm=12)[1]

    assert measured.configuration.options["psm"] == 11
    assert overridden.configuration.options["psm"] == 12
