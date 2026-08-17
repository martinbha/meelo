"""Repeating an acceptance must not repeat the money (#79, specification 6-7, 26).

Every path that turns reviewed evidence into financial history can be attempted
twice: a worker retried after a timeout, a double-clicked button, two tabs on one
queue. These tests hold each of them to producing exactly one canonical event.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db import transaction as db_transaction

from apps.core.errors import ForbiddenError
from apps.ledger.models import LedgerEntry
from apps.observations.models import ImportedObservation
from apps.observations.review import accept_observation
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.reconciliation.models import ReconciliationMatch
from apps.reconciliation.refunds import confirm_refund_match, propose_refund_matches
from apps.reconciliation.services import ReconciliationError, record_match
from apps.reconciliation.transfers import confirm_internal_transfer, propose_internal_transfers
from apps.transactions.idempotency import (
    OBSERVATION_SOURCE,
    REFUND_SOURCE,
    TRANSFER_SOURCE,
    save_once,
    source_key,
)
from apps.transactions.models import CanonicalTransaction
from tests.factories import (
    make_account,
    make_document,
    make_ledger_accounts,
    make_ocr_run,
    make_user,
)

pytestmark = pytest.mark.django_db

KEY = os.urandom(32)


@pytest.fixture
def owner() -> Any:
    return make_user(email="idempotency-owner@example.com")


def parsed(**overrides: Any) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 8, 15),
        "amount": Decimal("30000"),
        "currency": "KRW",
        "direction": TransactionDirection.DEBIT,
        "merchant": "이마트",
        "confidence_factors": {"token_confidence": 0.95, "amount_confidence": 0.95},
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def seed(user: Any, *observations: ParsedObservation, sha: str = "3" * 64) -> Any:
    document = make_document(user, file_sha256=sha)
    run = make_ocr_run(user, document)
    return import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            observations,
        ),
        data_key=KEY,
        key_version=1,
    ).observations


# ---------------------------------------------------------------------------
# The key itself
# ---------------------------------------------------------------------------


def test_a_key_names_its_origin_and_nothing_else() -> None:
    key = source_key(OBSERVATION_SOURCE, "3f2a")

    assert key == "observation:3f2a"


def test_a_key_needs_a_source() -> None:
    with pytest.raises(ValueError):
        source_key("", "3f2a")


def test_the_database_refuses_a_second_transaction_for_one_origin(owner: Any) -> None:
    """The backstop behind the row locks."""

    account = make_account(owner, name_blind_index="idem-account")
    shared = source_key(OBSERVATION_SOURCE, "abc")

    def build() -> CanonicalTransaction:
        return CanonicalTransaction(
            user=owner,
            created_by=owner,
            financial_account=account,
            occurred_at=date(2026, 8, 15),
            amount_encrypted="30000:KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
            source_idempotency_key=shared,
        )

    build().save()

    with pytest.raises(IntegrityError), db_transaction.atomic():
        build().save()


def test_transactions_without_a_key_are_never_deduplicated(owner: Any) -> None:
    """Two identical manual entries are a legitimate thing for a person to make."""

    account = make_account(owner, name_blind_index="idem-manual")
    for _ in range(2):
        CanonicalTransaction.objects.create(
            user=owner,
            created_by=owner,
            financial_account=account,
            occurred_at=date(2026, 8, 15),
            amount_encrypted="30000:KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        )

    assert CanonicalTransaction.objects.filter(user=owner).count() == 2


def test_two_users_may_share_a_key(owner: Any) -> None:
    """Keys are scoped per user; one person's retry is not another's."""

    stranger = make_user(email="idempotency-stranger@example.com")
    shared = source_key(OBSERVATION_SOURCE, "abc")
    for person in (owner, stranger):
        CanonicalTransaction.objects.create(
            user=person,
            created_by=person,
            financial_account=make_account(person, name_blind_index=f"idem-{person.pk}"),
            occurred_at=date(2026, 8, 15),
            amount_encrypted="30000:KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
            source_idempotency_key=shared,
        )

    assert CanonicalTransaction.objects.filter(source_idempotency_key=shared).count() == 2


def test_losing_the_race_returns_the_winners_transaction(owner: Any) -> None:
    """What a second process sees when it arrives without the lock."""

    account = make_account(owner, name_blind_index="idem-race")
    shared = source_key(OBSERVATION_SOURCE, "abc")
    winner = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="30000:KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        source_idempotency_key=shared,
    )

    resolved, created = save_once(
        CanonicalTransaction(
            user=owner,
            created_by=owner,
            financial_account=account,
            occurred_at=date(2026, 8, 15),
            amount_encrypted="30000:KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
            source_idempotency_key=shared,
        )
    )

    assert not created
    assert resolved.pk == winner.pk
    assert CanonicalTransaction.objects.filter(user=owner).count() == 1


def test_the_surrounding_transaction_survives_losing_the_race(owner: Any) -> None:
    """A savepoint, so a conflict does not poison work the caller still has."""

    account = make_account(owner, name_blind_index="idem-savepoint")
    shared = source_key(OBSERVATION_SOURCE, "abc")
    common = {
        "user": owner,
        "created_by": owner,
        "financial_account": account,
        "occurred_at": date(2026, 8, 15),
        "amount_encrypted": "30000:KRW",
        "transaction_type": CanonicalTransaction.TransactionType.PURCHASE,
    }
    CanonicalTransaction.objects.create(source_idempotency_key=shared, **common)

    with db_transaction.atomic():
        save_once(CanonicalTransaction(source_idempotency_key=shared, **common))
        # The connection is still usable, so this write lands.
        CanonicalTransaction.objects.create(**common)

    assert CanonicalTransaction.objects.filter(user=owner).count() == 2


# ---------------------------------------------------------------------------
# One canonical event per origin, whichever path made it
# ---------------------------------------------------------------------------


def test_a_retried_acceptance_converges_on_one_transaction(owner: Any) -> None:
    rows = seed(owner, parsed())
    account = make_account(owner, name_blind_index="idem-accept")
    context = make_ledger_accounts(owner, account, prefix="id")

    first = accept_observation(
        rows[0].pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        ledger_accounts=context,
    )
    second = accept_observation(
        rows[0].pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        ledger_accounts=context,
    )

    assert first.pk == second.pk
    assert CanonicalTransaction.objects.filter(user=owner).count() == 1
    # And the ledger was posted once, not twice.
    assert LedgerEntry.objects.filter(transaction=first).count() == 2


def test_an_acceptance_carries_the_key_of_the_row_that_produced_it(owner: Any) -> None:
    rows = seed(owner, parsed())
    account = make_account(owner, name_blind_index="idem-key")

    transaction = accept_observation(
        rows[0].pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    assert transaction.source_idempotency_key == source_key(OBSERVATION_SOURCE, rows[0].pk)


def test_an_acceptance_that_lost_the_race_still_links_the_row(owner: Any) -> None:
    """Whoever wins, the row must end up pointing at the surviving transaction."""

    rows = seed(owner, parsed())
    account = make_account(owner, name_blind_index="idem-linked")
    winner = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="30000:KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        source_idempotency_key=source_key(OBSERVATION_SOURCE, rows[0].pk),
    )

    resolved = accept_observation(
        rows[0].pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    rows[0].refresh_from_db()
    assert resolved.pk == winner.pk
    assert rows[0].canonical_transaction_id == winner.pk
    assert rows[0].review_status == ImportedObservation.ReviewStatus.ACCEPTED
    assert CanonicalTransaction.objects.filter(user=owner).count() == 1


def test_a_retried_transfer_converges_on_one_event(owner: Any) -> None:
    checking = make_account(owner, name_blind_index="idem-checking")
    savings = make_account(owner, name_blind_index="idem-savings")
    rows = seed(owner, parsed(), parsed(direction=TransactionDirection.CREDIT))
    rows[0].financial_account_guess = checking
    rows[0].save(update_fields=["financial_account_guess"])
    rows[1].financial_account_guess = savings
    rows[1].save(update_fields=["financial_account_guess"])
    match = propose_internal_transfers(user=owner, data_key=KEY)[0]

    first = confirm_internal_transfer(match.pk, user=owner, data_key=KEY)
    second = confirm_internal_transfer(match.pk, user=owner, data_key=KEY)

    assert first.pk == second.pk
    assert first.source_idempotency_key == source_key(TRANSFER_SOURCE, match.pk)
    assert CanonicalTransaction.objects.filter(user=owner).count() == 1


def test_a_retried_refund_converges_on_one_event(owner: Any) -> None:
    account = make_account(owner, name_blind_index="idem-refund")
    rows = seed(
        owner,
        parsed(),
        parsed(direction=TransactionDirection.CREDIT, occurred_on=date(2026, 8, 25)),
    )
    for row in rows:
        row.financial_account_guess = account
        row.save(update_fields=["financial_account_guess"])
    match = propose_refund_matches(user=owner, data_key=KEY)[0]

    first = confirm_refund_match(match.pk, user=owner, data_key=KEY)
    second = confirm_refund_match(match.pk, user=owner, data_key=KEY)

    assert first.pk == second.pk
    assert first.source_idempotency_key == source_key(REFUND_SOURCE, match.pk)
    assert CanonicalTransaction.objects.filter(user=owner).count() == 1


# ---------------------------------------------------------------------------
# A match cannot link incompatible users or accounts
# ---------------------------------------------------------------------------


def test_a_match_across_users_is_refused_by_the_service(owner: Any) -> None:
    mine = seed(owner, parsed())
    stranger = make_user(email="idempotency-intruder@example.com")
    theirs = seed(stranger, parsed(), sha="2" * 64)

    with pytest.raises(ForbiddenError):
        record_match(
            user=owner,
            left_observation_id=mine[0].pk,
            right_observation_id=theirs[0].pk,
            match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
            score=95,
        )


def test_a_match_across_users_is_refused_even_without_the_service(owner: Any) -> None:
    """The invariant has to hold for every caller, not only the careful ones."""

    mine = seed(owner, parsed())
    stranger = make_user(email="idempotency-bypass@example.com")
    theirs = seed(stranger, parsed(), sha="2" * 64)

    with pytest.raises(ValidationError):
        ReconciliationMatch.objects.create(
            user=owner,
            left_observation=mine[0],
            right_observation=theirs[0],
            match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
            match_score=95,
        )

    assert not ReconciliationMatch.objects.exists()


def test_a_transfer_between_incompatible_accounts_is_refused(owner: Any) -> None:
    """Both sides of an internal move must sit in different accounts you own."""

    account = make_account(owner, name_blind_index="idem-single")
    rows = seed(owner, parsed(), parsed(direction=TransactionDirection.CREDIT))
    for row in rows:
        row.financial_account_guess = account
        row.save(update_fields=["financial_account_guess"])
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.INTERNAL_TRANSFER,
        score=85,
    )

    with pytest.raises(ReconciliationError):
        confirm_internal_transfer(match.pk, user=owner, data_key=KEY)

    assert not CanonicalTransaction.objects.filter(user=owner).exists()


# ---------------------------------------------------------------------------
# Database-level integrity of merged rows
# ---------------------------------------------------------------------------


def test_a_merged_row_cannot_also_carry_a_transaction(owner: Any) -> None:
    """Both would report the same money twice."""

    rows = seed(owner, parsed(), parsed())
    account = make_account(owner, name_blind_index="idem-merged")
    transaction = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="30000:KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    with pytest.raises(IntegrityError), db_transaction.atomic():
        ImportedObservation.objects.filter(pk=rows[1].pk).update(
            merged_into=rows[0],
            review_status=ImportedObservation.ReviewStatus.MERGED,
            canonical_transaction=transaction,
        )


def test_a_merge_cannot_happen_without_the_status_saying_so(owner: Any) -> None:
    """The status is what keeps a merged row out of reports."""

    rows = seed(owner, parsed(), parsed())

    with pytest.raises(IntegrityError), db_transaction.atomic():
        ImportedObservation.objects.filter(pk=rows[1].pk).update(merged_into=rows[0])
