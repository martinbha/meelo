"""Emit human and machine-readable parser fixture accuracy reports."""

from __future__ import annotations

import argparse
from pathlib import Path

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


def build_registry() -> ParserRegistry:
    registry = ParserRegistry(generic_parser=GenericTransactionListParser())
    for parser in build_institution_parsers():
        registry.register(parser)
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_REPORT)
    arguments = parser.parse_args(argv)

    metrics = run_parser_fixture_suite(
        load_parser_fixtures(arguments.fixtures), registry=build_registry()
    )
    report = build_accuracy_report(metrics)
    arguments.json_output.write_text(report.to_json(), encoding="utf-8")
    print(summarize_report(report))
    print(f"json_report={arguments.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
