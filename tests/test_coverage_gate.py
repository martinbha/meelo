from __future__ import annotations

from scripts.check_coverage import check_coverage


def _report(*, total: float, module_totals: dict[str, float]) -> dict[str, object]:
    files: dict[str, object] = {}
    for module, percentage in module_totals.items():
        files[f"{module}/example.py"] = {
            "summary": {"covered_lines": percentage, "num_statements": 100}
        }
    return {"totals": {"percent_covered": total}, "files": files}


def test_coverage_gate_accepts_project_and_critical_floors() -> None:
    assert (
        check_coverage(
            _report(
                total=90,
                module_totals={
                    "apps/core": 91,
                    "apps/ledger": 91,
                    "apps/reconciliation": 91,
                    "apps/reports": 96,
                },
            )
        )
        == []
    )


def test_coverage_gate_reports_each_floor_that_is_breached() -> None:
    failures = check_coverage(
        _report(
            total=84,
            module_totals={
                "apps/core": 89,
                "apps/ledger": 89,
                "apps/reconciliation": 89,
                "apps/reports": 94,
            },
        )
    )
    assert failures == [
        "project coverage 84.0% is below 85.0%",
        "apps/core coverage 89.0% is below 90.0%",
        "apps/ledger coverage 89.0% is below 90.0%",
        "apps/reconciliation coverage 89.0% is below 90.0%",
        "apps/reports coverage 94.0% is below 95.0%",
    ]
