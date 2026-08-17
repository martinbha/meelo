"""Reconciliation scenarios expressed as sanitized fixtures.

A reconciliation bug does not look like a crash. It looks like a total that is
twice what it should be, or a month where a transfer between the user's own
accounts appears as both a payday and a shopping spree. Those failures survive
unit tests of the scoring functions, because each function is right on its own —
what goes wrong is the arithmetic after several of them agree.

So a scenario here describes whole screenshots, says which candidates detection
must find and how strongly, says how a reviewer resolves them, and then states
the totals that must come out the other end. The assertions that matter are the
last ones (specification 16-17, 31.3-31.4).

Fixtures are sanitized by construction: amounts and dates are invented, and
merchant names are the generic ones used throughout the test suite. Nothing here
comes from a real statement.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any


class ReconciliationFixtureError(ValueError):
    """A reconciliation scenario file cannot be loaded."""


#: Detection passes a scenario may ask for, named as the fixture spells them.
DETECTIONS = ("duplicates", "transfers", "refunds", "settlements")

#: Reviewer actions a scenario may apply to a candidate.
ACTIONS = ("merge", "confirm", "reject")


@dataclass(frozen=True, slots=True)
class FixtureRow:
    """One parsed row on one screenshot."""

    key: str
    occurred_on: date | None
    amount_minor: int | None
    currency: str
    direction: str
    merchant: str
    approval_code: str = ""
    balance_after_minor: int | None = None
    is_settlement: bool = False
    #: Account this row is mapped to, by fixture key. Unmapped rows exist on
    #: purpose: an unmapped side is what stops an external payment being read
    #: as an internal transfer.
    account: str | None = None


@dataclass(frozen=True, slots=True)
class FixtureDocument:
    """One screenshot, its parser, and the rows read off it."""

    key: str
    source_type: str
    parser: str
    rows: tuple[FixtureRow, ...]


@dataclass(frozen=True, slots=True)
class ExpectedCandidate:
    """A relationship detection is required to propose."""

    match_type: str
    left: str
    right: str
    minimum_score: int = 0
    maximum_score: int = 100
    features: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Resolution:
    """One reviewer decision on a proposed candidate."""

    candidate: int
    action: str
    winner: str | None = None


@dataclass(frozen=True, slots=True)
class Acceptance:
    """A row accepted on its own, outside any relationship."""

    row: str
    transaction_type: str


@dataclass(frozen=True, slots=True)
class ExpectedTotals:
    """What the books must say once every decision has been applied.

    ``spending_minor`` and ``refund_minor`` are kept apart rather than netted in
    the fixture, so a scenario states both the gross figure and the reduction
    and a bug that loses one of them cannot hide inside the difference.
    """

    canonical_events: int = 0
    ledger_entries: int = 0
    spending_minor: int = 0
    income_minor: int = 0
    refund_minor: int = 0
    neutral_minor: int = 0

    @property
    def net_spending_minor(self) -> int:
        return self.spending_minor - self.refund_minor


@dataclass(frozen=True, slots=True)
class ReconciliationScenario:
    """One end-to-end reconciliation case."""

    name: str
    description: str
    detect: str
    accounts: tuple[str, ...]
    documents: tuple[FixtureDocument, ...]
    expected_candidates: tuple[ExpectedCandidate, ...]
    resolution: tuple[Resolution, ...] = ()
    accept: tuple[Acceptance, ...] = ()
    expected: ExpectedTotals = field(default_factory=ExpectedTotals)

    @property
    def rows(self) -> tuple[FixtureRow, ...]:
        return tuple(row for document in self.documents for row in document.rows)


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReconciliationFixtureError(f"Expected an ISO date string, got {value!r}.")
    return date.fromisoformat(value)


def _row(payload: Mapping[str, Any]) -> FixtureRow:
    try:
        key = str(payload["key"])
    except KeyError as exc:
        raise ReconciliationFixtureError("Every row needs a key.") from exc
    return FixtureRow(
        key=key,
        occurred_on=_parse_date(payload.get("date")),
        amount_minor=(
            int(payload["amount_minor"]) if payload.get("amount_minor") is not None else None
        ),
        currency=str(payload.get("currency", "KRW")),
        direction=str(payload.get("direction", "unknown")),
        merchant=str(payload.get("merchant", "")),
        approval_code=str(payload.get("approval_code", "")),
        balance_after_minor=(
            int(payload["balance_after_minor"])
            if payload.get("balance_after_minor") is not None
            else None
        ),
        is_settlement=bool(payload.get("is_settlement", False)),
        account=str(payload["account"]) if payload.get("account") is not None else None,
    )


def _document(payload: Mapping[str, Any]) -> FixtureDocument:
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ReconciliationFixtureError("Every document needs at least one row.")
    return FixtureDocument(
        key=str(payload.get("key", "")),
        source_type=str(payload.get("source_type", "unknown")),
        parser=str(payload.get("parser", "toss_bank")),
        rows=tuple(_row(item) for item in rows),
    )


def _candidate(payload: Mapping[str, Any]) -> ExpectedCandidate:
    return ExpectedCandidate(
        match_type=str(payload["type"]),
        left=str(payload["left"]),
        right=str(payload["right"]),
        minimum_score=int(payload.get("minimum_score", 0)),
        maximum_score=int(payload.get("maximum_score", 100)),
        features=tuple(str(name) for name in payload.get("features", ())),
    )


def _resolution(payload: Mapping[str, Any]) -> Resolution:
    action = str(payload.get("action", ""))
    if action not in ACTIONS:
        raise ReconciliationFixtureError(f"Unknown reviewer action: {action!r}.")
    winner = payload.get("winner")
    if action == "merge" and winner is None:
        raise ReconciliationFixtureError("A merge has to say which row survives.")
    return Resolution(
        candidate=int(payload.get("candidate", 0)),
        action=action,
        winner=str(winner) if winner is not None else None,
    )


def _totals(payload: Mapping[str, Any]) -> ExpectedTotals:
    return ExpectedTotals(
        canonical_events=int(payload.get("canonical_events", 0)),
        ledger_entries=int(payload.get("ledger_entries", 0)),
        spending_minor=int(payload.get("spending_minor", 0)),
        income_minor=int(payload.get("income_minor", 0)),
        refund_minor=int(payload.get("refund_minor", 0)),
        neutral_minor=int(payload.get("neutral_minor", 0)),
    )


def load_scenario(path: Path) -> ReconciliationScenario:
    """Load one scenario file, failing loudly on anything it cannot express."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    detect = str(payload.get("detect", ""))
    if detect not in DETECTIONS:
        raise ReconciliationFixtureError(f"Unknown detection pass: {detect!r}.")
    documents = payload.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ReconciliationFixtureError("A scenario needs at least one document.")

    scenario = ReconciliationScenario(
        name=str(payload.get("name", path.stem)),
        description=str(payload.get("description", "")),
        detect=detect,
        accounts=tuple(str(name) for name in payload.get("accounts", ())),
        documents=tuple(_document(item) for item in documents),
        expected_candidates=tuple(_candidate(item) for item in payload.get("candidates", ())),
        resolution=tuple(_resolution(item) for item in payload.get("resolution", ())),
        accept=tuple(
            Acceptance(row=str(item["row"]), transaction_type=str(item["type"]))
            for item in payload.get("accept", ())
        ),
        expected=_totals(payload.get("expected", {})),
    )
    _validate(scenario)
    return scenario


def _validate(scenario: ReconciliationScenario) -> None:
    """Catch a fixture that refers to rows or accounts it never declared.

    A scenario with a typo in a row key would otherwise assert nothing and pass.
    """

    keys = {row.key for row in scenario.rows}
    if len(keys) != len(scenario.rows):
        raise ReconciliationFixtureError("Row keys must be unique across the scenario.")
    accounts = set(scenario.accounts)
    for row in scenario.rows:
        if row.account is not None and row.account not in accounts:
            raise ReconciliationFixtureError(f"Row {row.key!r} maps to an undeclared account.")
    for candidate in scenario.expected_candidates:
        for side in (candidate.left, candidate.right):
            if side not in keys:
                raise ReconciliationFixtureError(f"Candidate refers to unknown row {side!r}.")
    for step in scenario.resolution:
        if not 0 <= step.candidate < len(scenario.expected_candidates):
            raise ReconciliationFixtureError(f"Resolution names candidate {step.candidate}.")
        if step.winner is not None and step.winner not in keys:
            raise ReconciliationFixtureError(f"Resolution names unknown row {step.winner!r}.")
    for acceptance in scenario.accept:
        if acceptance.row not in keys:
            raise ReconciliationFixtureError(f"Acceptance names unknown row {acceptance.row!r}.")


def load_scenarios(root: Path) -> tuple[ReconciliationScenario, ...]:
    """Load every scenario under ``root``, in a stable order."""

    resolved = root.resolve()
    return tuple(load_scenario(path) for path in sorted(resolved.glob("*.json")))


def parsed_observations(document: FixtureDocument) -> Sequence[Any]:
    """Turn a fixture document's rows into parser output.

    Built through the real :class:`~apps.parsing.contracts.ParsedObservation`
    so the scenario goes through the same import path a screenshot does, rather
    than writing rows straight into the database.
    """

    from apps.parsing.contracts import ParsedObservation, TransactionDirection

    directions = {
        "debit": TransactionDirection.DEBIT,
        "credit": TransactionDirection.CREDIT,
    }
    return [
        ParsedObservation(
            occurred_on=row.occurred_on,
            amount=Decimal(row.amount_minor) if row.amount_minor is not None else None,
            currency=row.currency,
            direction=directions.get(row.direction),
            merchant=row.merchant,
            approval_code=row.approval_code or None,
            balance_after=(
                Decimal(row.balance_after_minor) if row.balance_after_minor is not None else None
            ),
            is_settlement=row.is_settlement,
            confidence_factors={"token_confidence": 0.95, "amount_confidence": 0.95},
            parser_name=document.parser,
            parser_version="1.0",
            parser_support_score=0.95,
        )
        for row in document.rows
    ]
