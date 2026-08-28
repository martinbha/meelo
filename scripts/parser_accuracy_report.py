"""Emit human and machine-readable parser fixture accuracy reports."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from apps.parsing.fixture_harness import (
    build_accuracy_report,
    load_parser_fixtures,
    run_parser_fixture_suite,
    summarize_report,
)
from apps.parsing.generic import GenericTransactionListParser
from apps.parsing.institutions import build_institution_parsers
from apps.parsing.registry import ParserRegistry

DEFAULT_FIXTURES = Path("tests/fixtures/parsers")
DEFAULT_JSON_REPORT = Path("parser-accuracy-report.json")
DEFAULT_BASELINE = Path("tests/parser_accuracy_baselines.json")
MINIMUM_METRICS = ("amount_accuracy", "date_accuracy", "merchant_accuracy")
MAXIMUM_METRICS = ("missed_rate", "false_rate")


def build_registry() -> ParserRegistry:
    registry = ParserRegistry(generic_parser=GenericTransactionListParser())
    for parser in build_institution_parsers():
        registry.register(parser)
    return registry


def baseline_failures(measured: Mapping[str, Any], baseline: Mapping[str, Any]) -> tuple[str, ...]:
    """Return deterministic failures when institution metrics regress."""
    measured_groups = measured.get("by_institution", {})
    baseline_groups = baseline.get("by_institution", {})
    if not isinstance(measured_groups, Mapping) or not isinstance(baseline_groups, Mapping):
        return ("reports must contain a by_institution mapping",)

    failures: list[str] = []
    for institution in sorted(set(measured_groups) | set(baseline_groups)):
        actual = measured_groups.get(institution)
        expected = baseline_groups.get(institution)
        if not isinstance(actual, Mapping):
            failures.append(f"{institution}: baseline has no measured institution")
            continue
        if not isinstance(expected, Mapping):
            failures.append(f"{institution}: measured institution has no baseline")
            continue
        for metric in MINIMUM_METRICS:
            value = float(actual[metric])
            threshold = float(expected[metric])
            if value < threshold:
                failures.append(f"{institution}.{metric}: {value:.4f} < {threshold:.4f}")
        for metric in MAXIMUM_METRICS:
            value = float(actual[metric])
            threshold = float(expected[metric])
            if value > threshold:
                failures.append(f"{institution}.{metric}: {value:.4f} > {threshold:.4f}")
    return tuple(failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    arguments = parser.parse_args(argv)

    metrics = run_parser_fixture_suite(
        load_parser_fixtures(arguments.fixtures), registry=build_registry()
    )
    report = build_accuracy_report(metrics)
    arguments.json_output.write_text(report.to_json(), encoding="utf-8")
    print(summarize_report(report))
    print(f"json_report={arguments.json_output}")
    baseline = json.loads(arguments.baseline.read_text(encoding="utf-8"))
    failures = baseline_failures(report.as_dict(), baseline)
    for failure in failures:
        print(f"regression={failure}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
