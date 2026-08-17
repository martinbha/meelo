"""Reconciliation scenarios driven end to end (#80, specification 16-17, 31.3-31.4).

A reconciliation bug does not look like a crash. It looks like a month that is
twice what it should be, or a transfer between a user's own accounts appearing as
both a payday and a shopping spree. Unit tests of the scoring functions miss
those, because each function is right on its own — what goes wrong is the
arithmetic after several of them agree.

So each scenario here runs the real import, the real detection, the real reviewer
actions, and then checks the totals. Every scenario is also run twice, because a
worker that reruns detection must not change any answer.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from apps.ledger.models import LedgerEntry
from apps.observations.models import ImportedObservation
from apps.observations.review import accept_observation, decrypt_observation
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import ParserMetadata, ParserSupport
from apps.parsing.registry import ParserSelection
from apps.reconciliation.duplicates import find_duplicate_candidates
from apps.reconciliation.fixture_harness import (
    ReconciliationFixtureError,
    ReconciliationScenario,
    load_scenario,
    load_scenarios,
    parsed_observations,
)
from apps.reconciliation.matching import match_credit_card_settlement
from apps.reconciliation.models import ReconciliationMatch
from apps.reconciliation.refunds import confirm_refund_match, propose_refund_matches
from apps.reconciliation.services import (
    confirm_duplicate_match,
    confirm_match,
    decrypt_match_features,
    facts_from,
    record_duplicate_candidates,
    record_proposals,
    reject_match,
)
from apps.reconciliation.transfers import confirm_internal_transfer, propose_internal_transfers
from apps.transactions.classification import (
    is_income,
    is_neutral,
    is_spending,
    is_spending_reduction,
)
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import read_money
from tests.factories import (
    make_account,
    make_document,
    make_ledger_accounts,
    make_ocr_run,
    make_user,
)

pytestmark = pytest.mark.django_db

KEY = os.urandom(32)
#: Duplicate grouping is keyed; the value is low entropy without one.
SEARCH_KEY = os.urandom(32)

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "reconciliation"

SCENARIOS = load_scenarios(FIXTURE_ROOT)


def scenario_ids() -> list[str]:
    return [scenario.name for scenario in SCENARIOS]


# ---------------------------------------------------------------------------
# Driving one scenario
# ---------------------------------------------------------------------------


def build_world(scenario: ReconciliationScenario, user: Any) -> dict[str, Any]:
    """Create the accounts, screenshots, and observations a scenario describes."""

    accounts = {
        name: make_account(user, name_blind_index=f"{scenario.name}-{name}")
        for name in scenario.accounts
    }
    rows: dict[str, ImportedObservation] = {}
    for index, document in enumerate(scenario.documents):
        stored = make_document(user, file_sha256=f"{index:064x}")
        run = make_ocr_run(user, stored)
        imported = import_parser_selection(
            document=stored,
            ocr_run=run,
            selection=ParserSelection(
                ParserMetadata(document.parser, "1.0"),
                ParserSupport(0.95, document.source_type, ()),
                tuple(parsed_observations(document)),
            ),
            data_key=KEY,
            key_version=1,
        ).observations
        for fixture_row, observation in zip(document.rows, imported, strict=True):
            if fixture_row.account is not None:
                observation.financial_account_guess = accounts[fixture_row.account]
                observation.save(update_fields=["financial_account_guess"])
            rows[fixture_row.key] = observation
    # One chart per account, built once: repeating a scenario must reuse it
    # rather than create a second chart with the same name.
    ledger = (
        {
            account.pk: make_ledger_accounts(user, account, prefix=f"{scenario.name}-{name}")
            for name, account in accounts.items()
        }
        if scenario.expected.ledger_entries
        else {}
    )
    return {"accounts": accounts, "rows": rows, "ledger": ledger}


def observation_facts(scenario: ReconciliationScenario, world: dict[str, Any], user: Any) -> Any:
    source_types = {
        row.key: document.source_type for document in scenario.documents for row in document.rows
    }
    facts = {}
    for key, observation in world["rows"].items():
        values = decrypt_observation(observation, user=user, data_key=KEY)
        facts[key] = facts_from(
            observation,
            merchant=values.merchant,
            amount_minor=values.amount_minor,
            approval_code=values.approval_code,
            balance_after_minor=(
                values.balance_after.amount_minor if values.balance_after is not None else None
            ),
            source_type=source_types[key],
        )
    return facts


def detect(scenario: ReconciliationScenario, world: dict[str, Any], user: Any) -> Any:
    """Run the detection pass the scenario asks for."""

    if scenario.detect == "transfers":
        return propose_internal_transfers(user=user, data_key=KEY)
    if scenario.detect == "refunds":
        return propose_refund_matches(user=user, data_key=KEY)

    facts = observation_facts(scenario, world, user)
    if scenario.detect == "duplicates":
        return record_duplicate_candidates(
            user=user,
            candidates=find_duplicate_candidates(list(facts.values()), search_key=SEARCH_KEY),
            data_key=KEY,
        )

    # Settlements: the caller pairs bank rows against statement rows, because
    # the matcher does not know which screenshot a row came from. Every
    # combination is tried rather than only the ones the fixture names, so a
    # proposal nobody expected still shows up and fails the count.
    statements = [
        row.key
        for document in scenario.documents
        if document.source_type == "card_statement"
        for row in document.rows
    ]
    withdrawals = [
        row.key
        for document in scenario.documents
        if document.source_type != "card_statement"
        for row in document.rows
    ]
    proposals = []
    for withdrawal in withdrawals:
        for statement in statements:
            proposal = match_credit_card_settlement(
                facts[withdrawal],
                facts[statement],
                statement_balance_minor=facts[statement].amount_minor,
            )
            if proposal is not None:
                proposals.append(proposal)
    return record_proposals(user=user, proposals=proposals, data_key=KEY)


def resolve(scenario: ReconciliationScenario, world: dict[str, Any], user: Any) -> None:
    """Apply the reviewer decisions the scenario records."""

    stored = list(
        ReconciliationMatch.objects.filter(user=user).order_by("-match_score", "created_at")
    )
    for step in scenario.resolution:
        expected = scenario.expected_candidates[step.candidate]
        match = next(
            item
            for item in stored
            if item.match_type == expected.match_type
            and {item.left_observation_id, item.right_observation_id}
            == {world["rows"][expected.left].pk, world["rows"][expected.right].pk}
        )
        if step.action == "reject":
            reject_match(match.pk, user=user)
        elif step.action == "merge":
            confirm_duplicate_match(
                match.pk, user=user, winner_id=world["rows"][step.winner or ""].pk
            )
        elif match.match_type == ReconciliationMatch.MatchType.INTERNAL_TRANSFER:
            confirm_internal_transfer(match.pk, user=user, data_key=KEY)
        elif match.match_type == ReconciliationMatch.MatchType.REFUND_MATCH:
            confirm_refund_match(match.pk, user=user, data_key=KEY)
        else:
            confirm_match(match.pk, user=user)


def accept_rows(scenario: ReconciliationScenario, world: dict[str, Any], user: Any) -> None:
    for acceptance in scenario.accept:
        observation = world["rows"][acceptance.row]
        observation.refresh_from_db()
        account = observation.financial_account_guess or next(iter(world["accounts"].values()))
        accept_observation(
            observation.pk,
            user=user,
            data_key=KEY,
            financial_account=account,
            transaction_type=acceptance.transaction_type,
            ledger_accounts=world["ledger"].get(account.pk),
            confirmed=True,
        )


def totals(user: Any) -> dict[str, int]:
    """Add up the confirmed history the way reporting will have to."""

    result = {"spending_minor": 0, "income_minor": 0, "refund_minor": 0, "neutral_minor": 0}
    for transaction in CanonicalTransaction.objects.filter(user=user):
        # Read through the same key-aware reader reporting uses. The amounts are
        # encrypted now, so a plain decode would see ciphertext.
        minor = read_money(transaction, "amount_encrypted", data_key=KEY).amount_minor
        kind = transaction.transaction_type
        if is_spending(kind):
            result["spending_minor"] += minor
        elif is_income(kind):
            result["income_minor"] += minor
        elif is_spending_reduction(kind):
            result["refund_minor"] += minor
        elif is_neutral(kind):
            result["neutral_minor"] += minor
    return result


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------


def test_the_fixture_directory_is_not_empty() -> None:
    """A silently empty fixture set would make every scenario test vacuous."""

    assert SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_ids())
def test_detection_proposes_exactly_what_the_scenario_expects(
    scenario: ReconciliationScenario,
) -> None:
    user = make_user(email=f"fixture-{scenario.name}@example.com")
    world = build_world(scenario, user)

    detect(scenario, world, user)

    stored = list(ReconciliationMatch.objects.filter(user=user))
    assert len(stored) == len(scenario.expected_candidates), scenario.description
    for expected in scenario.expected_candidates:
        pair = {world["rows"][expected.left].pk, world["rows"][expected.right].pk}
        match = next(
            item
            for item in stored
            if item.match_type == expected.match_type
            and {item.left_observation_id, item.right_observation_id} == pair
        )
        assert expected.minimum_score <= match.match_score <= expected.maximum_score
        features = set(decrypt_match_features(match, data_key=KEY))
        assert set(expected.features) <= features, sorted(features)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_ids())
def test_rerunning_detection_changes_nothing(scenario: ReconciliationScenario) -> None:
    """A retried worker must not pile up a second copy of every proposal."""

    user = make_user(email=f"rerun-{scenario.name}@example.com")
    world = build_world(scenario, user)

    detect(scenario, world, user)
    before = {
        (match.pk, match.match_score) for match in ReconciliationMatch.objects.filter(user=user)
    }
    detect(scenario, world, user)
    after = {
        (match.pk, match.match_score) for match in ReconciliationMatch.objects.filter(user=user)
    }

    assert before == after


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_ids())
def test_the_books_add_up_after_every_decision(scenario: ReconciliationScenario) -> None:
    user = make_user(email=f"totals-{scenario.name}@example.com")
    world = build_world(scenario, user)

    detect(scenario, world, user)
    resolve(scenario, world, user)
    accept_rows(scenario, world, user)

    expected = scenario.expected
    assert CanonicalTransaction.objects.filter(user=user).count() == expected.canonical_events, (
        scenario.description
    )
    assert totals(user) == {
        "spending_minor": expected.spending_minor,
        "income_minor": expected.income_minor,
        "refund_minor": expected.refund_minor,
        "neutral_minor": expected.neutral_minor,
    }, scenario.description
    if expected.ledger_entries:
        assert LedgerEntry.objects.filter(transaction__user=user).count() == expected.ledger_entries


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_ids())
def test_repeating_every_decision_changes_nothing(scenario: ReconciliationScenario) -> None:
    """Retries after a timeout converge rather than doubling the books."""

    user = make_user(email=f"repeat-{scenario.name}@example.com")
    world = build_world(scenario, user)

    detect(scenario, world, user)
    resolve(scenario, world, user)
    accept_rows(scenario, world, user)
    once = totals(user)
    accept_rows(scenario, world, user)

    assert totals(user) == once
    assert (
        CanonicalTransaction.objects.filter(user=user).count() == scenario.expected.canonical_events
    )


# ---------------------------------------------------------------------------
# The fixture format itself
# ---------------------------------------------------------------------------


def test_a_scenario_naming_a_row_that_does_not_exist_is_rejected(tmp_path: Path) -> None:
    """A typo in a row key would otherwise assert nothing and pass."""

    path = tmp_path / "broken.json"
    path.write_text(
        """
        {
          "name": "broken",
          "detect": "duplicates",
          "accounts": ["checking"],
          "documents": [
            {"key": "d", "source_type": "bank_transaction_list", "parser": "toss_bank",
             "rows": [{"key": "a", "date": "2026-08-15", "amount_minor": 1,
                       "direction": "debit", "account": "checking"}]}
          ],
          "candidates": [{"type": "duplicate_observation", "left": "a", "right": "typo"}]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ReconciliationFixtureError):
        load_scenario(path)


def test_a_scenario_mapping_a_row_to_an_undeclared_account_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text(
        """
        {
          "name": "broken",
          "detect": "duplicates",
          "accounts": ["checking"],
          "documents": [
            {"key": "d", "source_type": "bank_transaction_list", "parser": "toss_bank",
             "rows": [{"key": "a", "date": "2026-08-15", "amount_minor": 1,
                       "direction": "debit", "account": "savings"}]}
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ReconciliationFixtureError):
        load_scenario(path)


def test_an_unknown_detection_pass_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text(
        """
        {
          "name": "broken",
          "detect": "telepathy",
          "documents": [
            {"key": "d", "source_type": "bank_transaction_list", "parser": "toss_bank",
             "rows": [{"key": "a", "direction": "debit"}]}
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ReconciliationFixtureError):
        load_scenario(path)


def test_a_merge_must_say_which_row_survives(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text(
        """
        {
          "name": "broken",
          "detect": "duplicates",
          "accounts": ["checking"],
          "documents": [
            {"key": "d", "source_type": "bank_transaction_list", "parser": "toss_bank",
             "rows": [{"key": "a", "date": "2026-08-15", "amount_minor": 1,
                       "direction": "debit", "account": "checking"}]}
          ],
          "candidates": [{"type": "duplicate_observation", "left": "a", "right": "a"}],
          "resolution": [{"candidate": 0, "action": "merge"}]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ReconciliationFixtureError):
        load_scenario(path)
