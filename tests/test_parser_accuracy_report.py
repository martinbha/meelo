"""Regression coverage for parser accuracy report aggregation."""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from apps.parsing.contracts import (
    DocumentMetadata,
    NormalizedToken,
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    ScreenshotParser,
)
from apps.parsing.fixture_harness import (
    build_accuracy_report,
    load_parser_fixtures,
    run_parser_fixture_suite,
    summarize_report,
)
from apps.parsing.generic import GenericTransactionListParser
from apps.parsing.institutions import TossBankParser, build_institution_parsers
from apps.parsing.registry import ParserRegistry
from scripts.parser_accuracy_report import main

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "parsers"


def registry() -> ParserRegistry:
    result = ParserRegistry(generic_parser=GenericTransactionListParser())
    for parser in build_institution_parsers():
        result.register(parser)
    return result


class BrokenMerchantParser(ScreenshotParser):
    """A deliberately regressed parser used to prove metric sensitivity."""

    delegate = TossBankParser()

    @property
    def metadata(self) -> ParserMetadata:
        return self.delegate.metadata

    def supports(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> ParserSupport:
        return self.delegate.supports(document, tokens)

    def parse(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> tuple[ParsedObservation, ...]:
        observations = self.delegate.parse(document, tokens)
        return (replace(observations[0], merchant="regressed merchant"), *observations[1:])


def test_report_aggregates_from_manifests_at_every_level() -> None:
    metrics = run_parser_fixture_suite(load_parser_fixtures(FIXTURE_ROOT), registry=registry())

    report = build_accuracy_report(metrics)

    assert report.overall.fixtures == len(metrics)
    assert report.overall.expected_count == sum(item.expected_count for item in metrics)
    assert set(report.by_parser) == {item.parser for item in metrics}
    assert set(report.by_institution) == {item.institution for item in metrics}
    assert report.overall.amount_accuracy == 1.0
    assert report.overall.date_accuracy == 1.0
    assert report.overall.merchant_accuracy == 1.0
    assert report.overall.missed_rate == 0.0
    assert report.overall.false_rate == 0.0


def test_deliberately_broken_parser_lowers_the_expected_metric() -> None:
    case = next(
        item
        for item in load_parser_fixtures(FIXTURE_ROOT, institution="toss_bank")
        if item.name == "toss-bank-transaction-list"
    )
    broken_registry = ParserRegistry(generic_parser=GenericTransactionListParser())
    broken_registry.register(BrokenMerchantParser())

    report = build_accuracy_report(run_parser_fixture_suite((case,), registry=broken_registry))

    assert report.overall.amount_accuracy == 1.0
    assert report.overall.date_accuracy == 1.0
    assert report.overall.merchant_accuracy == 0.5
    assert report.overall.missed_rate == 0.0
    assert report.overall.false_rate == 0.0


def test_human_and_json_reports_are_stable_across_runs() -> None:
    metrics = run_parser_fixture_suite(load_parser_fixtures(FIXTURE_ROOT), registry=registry())
    first = build_accuracy_report(metrics)
    second = build_accuracy_report(tuple(reversed(metrics)))

    assert first.to_json() == second.to_json()
    assert summarize_report(first) == summarize_report(second)
    assert first.to_json().endswith("\n")
    assert '"schema_version": 1' in first.to_json()
    assert "overall fixtures=10" in summarize_report(first)


def test_report_command_emits_human_and_machine_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "accuracy.json"

    assert main(["--fixtures", str(FIXTURE_ROOT), "--json-output", str(output)]) == 0

    captured = capsys.readouterr()
    assert "overall fixtures=10" in captured.out
    assert f"json_report={output}" in captured.out
    assert output.read_text(encoding="utf-8").startswith("{\n")
