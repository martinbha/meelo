"""Removing a transaction without leaving a hole in the books (#153).

"Delete" is what a person calls it; the row survives, its ledger entries survive,
and an opposing entry is written for each. So these tests check what the books
say afterwards rather than what is missing from them: every account this
transaction touched is back where it started, the transaction is out of every
report, and the screenshot rows that fed it are back in the queue.
"""

from __future__ import annotations

import base64
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse

from apps.core.errors import ConflictError, InvalidRequestError
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.core.models import AuditEvent
from apps.core.value_objects import Money
from apps.ledger.models import LedgerEntry
from apps.ledger.posting import (
    Posting,
    entry_amount,
    post_balanced_transaction,
    reverse_transaction_postings,
    transaction_net_minor,
)
from apps.ledger.rules import PostingRuleAccounts
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
from apps.reconciliation.services import confirm_match, record_match, unlink_match
from apps.reconciliation.transfers import confirm_internal_transfer
from apps.reports.spending import monthly_spending, reportable_transactions
from apps.transactions.deletion import DeletionNotConfirmedError, delete_transaction
from apps.transactions.lifecycle import transition_transaction_status
from apps.transactions.models import CanonicalTransaction
from tests.factories import (
    make_account,
    make_document,
    make_ledger_accounts,
    make_ocr_run,
    make_transaction,
    make_user,
)

pytestmark = pytest.mark.django_db

PASSWORD = "deletion-password"


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="deletion-owner@example.com", password=PASSWORD)
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def account(owner: Any) -> Any:
    return make_account(owner)


def posted_transaction(owner: Any, account: Any, *, minor: int = 4200) -> Any:
    """A confirmed transaction with a balanced two-entry posting behind it."""

    transaction = make_transaction(owner, account, amount_encrypted=f"{minor}:KRW")
    transaction = transition_transaction_status(
        transaction.pk, user=owner, status=CanonicalTransaction.Status.CONFIRMED
    )
    accounts = make_ledger_accounts(owner, account, prefix=f"del{minor}")
    post_balanced_transaction(
        transaction,
        [
            Posting(accounts.offset, LedgerEntry.EntryType.DEBIT, Money(minor, "KRW")),
            Posting(accounts.account, LedgerEntry.EntryType.CREDIT, Money(minor, "KRW")),
        ],
    )
    return transaction, accounts


def account_position(ledger_account: Any, *, data_key: bytes | None = None) -> int:
    """Debits minus credits against one ledger account."""

    total = 0
    for entry in LedgerEntry.objects.filter(account=ledger_account):
        amount = entry_amount(entry, data_key=data_key).amount_minor
        total += amount if entry.entry_type == LedgerEntry.EntryType.DEBIT else -amount
    return total


def transfer_rows(user: Any, data_key: bytes) -> tuple[Any, Any, Any, Any]:
    source_account = make_account(user, name_blind_index="deletion-transfer-source")
    destination_account = make_account(user, name_blind_index="deletion-transfer-destination")
    document = make_document(user, file_sha256="8" * 64)
    run = make_ocr_run(user, document)
    rows = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (
                ParsedObservation(
                    occurred_on=date(2026, 8, 15),
                    amount=Decimal("500000"),
                    currency="KRW",
                    direction=TransactionDirection.DEBIT,
                    merchant="정기이체",
                    confidence_factors={"token_confidence": 0.95, "amount_confidence": 0.95},
                    parser_name="toss_bank",
                    parser_version="1.0",
                    parser_support_score=0.95,
                ),
                ParsedObservation(
                    occurred_on=date(2026, 8, 15),
                    amount=Decimal("500000"),
                    currency="KRW",
                    direction=TransactionDirection.CREDIT,
                    merchant="정기이체",
                    confidence_factors={"token_confidence": 0.95, "amount_confidence": 0.95},
                    parser_name="toss_bank",
                    parser_version="1.0",
                    parser_support_score=0.95,
                ),
            ),
        ),
        data_key=data_key,
        key_version=user.encryption_key_version,
    ).observations
    outgoing, incoming = rows
    outgoing.financial_account_guess = source_account
    incoming.financial_account_guess = destination_account
    outgoing.save(update_fields=["financial_account_guess"])
    incoming.save(update_fields=["financial_account_guess"])
    return outgoing, incoming, source_account, destination_account


# ----------------------------------------------------------------------
# The ledger
# ----------------------------------------------------------------------


def test_deleting_leaves_every_touched_account_at_zero(owner: Any, account: Any) -> None:
    transaction, accounts = posted_transaction(owner, account)
    assert account_position(accounts.offset) == 4200
    assert account_position(accounts.account) == -4200

    result = delete_transaction(transaction.pk, user=owner, confirmed=True)

    assert result.reversal_entry_count == 2
    assert transaction_net_minor(result.transaction) == 0
    # Per account, not only in total: a reversal that hit the wrong account
    # would still net to zero across the transaction.
    assert account_position(accounts.offset) == 0
    assert account_position(accounts.account) == 0


def test_the_original_entries_survive_the_reversal(owner: Any, account: Any) -> None:
    """The books have to be able to explain themselves afterwards."""

    transaction, _ = posted_transaction(owner, account)
    delete_transaction(transaction.pk, user=owner, confirmed=True)

    assert LedgerEntry.objects.filter(transaction=transaction).count() == 4


def test_a_transaction_with_no_postings_is_still_deletable(owner: Any, account: Any) -> None:
    transaction = make_transaction(owner, account)

    result = delete_transaction(transaction.pk, user=owner, confirmed=True)

    assert result.reversal_entry_count == 0
    assert result.transaction.status == CanonicalTransaction.Status.VOIDED


def test_reversing_twice_is_refused(owner: Any, account: Any) -> None:
    """A second pass balances the same accounts and doubles the noise."""

    transaction, _ = posted_transaction(owner, account)
    reverse_transaction_postings(transaction)

    with pytest.raises(ConflictError, match="already been reversed"):
        reverse_transaction_postings(transaction)
    assert LedgerEntry.objects.filter(transaction=transaction).count() == 4


def test_deleting_twice_is_refused(owner: Any, account: Any) -> None:
    transaction, _ = posted_transaction(owner, account)
    delete_transaction(transaction.pk, user=owner, confirmed=True)

    with pytest.raises(ConflictError, match="already been deleted"):
        delete_transaction(transaction.pk, user=owner, confirmed=True)
    assert LedgerEntry.objects.filter(transaction=transaction).count() == 4


# ----------------------------------------------------------------------
# Reporting and observations
# ----------------------------------------------------------------------


def test_a_deleted_transaction_leaves_the_reports(owner: Any, account: Any) -> None:
    kept = make_transaction(owner, account, amount_encrypted="100:KRW")
    removed, _ = posted_transaction(owner, account)

    delete_transaction(removed.pk, user=owner, confirmed=True)

    assert list(reportable_transactions(owner)) == [kept]


def test_linked_observations_return_to_the_review_queue(owner: Any) -> None:
    document = make_document(owner, file_sha256="7" * 64)
    run = make_ocr_run(owner, document)
    key = os.urandom(32)
    rows = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (
                ParsedObservation(
                    occurred_on=date(2026, 8, 15),
                    amount=Decimal("4200"),
                    currency="KRW",
                    direction=TransactionDirection.DEBIT,
                    merchant="스타벅스",
                    confidence_factors={
                        "token_confidence": 0.95,
                        "date_confidence": 1.0,
                        "amount_confidence": 0.98,
                        "direction_confidence": 0.95,
                    },
                    parser_name="toss_bank",
                    parser_version="1.0",
                    parser_support_score=0.95,
                ),
            ),
        ),
        data_key=key,
        key_version=1,
        blind_index_key=os.urandom(32),
        actor=owner,
    ).observations
    row = rows[0]
    ledger_account = make_account(owner, name_blind_index="deletion-observed")
    canonical = accept_observation(
        row.pk,
        user=owner,
        data_key=key,
        financial_account=ledger_account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    row.refresh_from_db()
    assert row.review_status == ImportedObservation.ReviewStatus.ACCEPTED
    assert row.feeds_reports is True

    result = delete_transaction(canonical.pk, user=owner, confirmed=True, data_key=key)

    row.refresh_from_db()
    assert result.released_observation_count == 1
    assert row.canonical_transaction_id is None
    assert row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
    assert row.reviewed_by_id is None
    assert row.is_open is True
    assert row.feeds_reports is False


# ----------------------------------------------------------------------
# Confirmation, ownership, audit
# ----------------------------------------------------------------------


def test_deletion_without_confirmation_changes_nothing(owner: Any, account: Any) -> None:
    transaction, accounts = posted_transaction(owner, account)

    with pytest.raises(DeletionNotConfirmedError):
        delete_transaction(transaction.pk, user=owner)

    transaction.refresh_from_db()
    assert transaction.status == CanonicalTransaction.Status.CONFIRMED
    assert account_position(accounts.offset) == 4200
    assert LedgerEntry.objects.filter(transaction=transaction).count() == 2


def test_deletion_is_refused_for_another_users_transaction(owner: Any, account: Any) -> None:
    intruder = make_user(email="deletion-intruder@example.com", password=PASSWORD)
    transaction, accounts = posted_transaction(owner, account)

    with pytest.raises(InvalidRequestError):
        delete_transaction(transaction.pk, user=intruder, confirmed=True)

    transaction.refresh_from_db()
    assert transaction.status == CanonicalTransaction.Status.CONFIRMED
    assert account_position(accounts.offset) == 4200
    assert not intruder.audit_events.exists()


def test_deletion_is_audited_with_counts_and_no_amounts(owner: Any, account: Any) -> None:
    transaction, _ = posted_transaction(owner, account)

    delete_transaction(
        transaction.pk, user=owner, confirmed=True, reason="Duplicate of the card row."
    )

    event = owner.audit_events.filter(event_type="transaction_deleted").get()
    assert event.metadata["reversal_entry_count"] == 2
    assert event.metadata["reason"] == "Duplicate of the card row."
    assert "4200" not in str(event.metadata)
    # Voiding through deletion is recorded as both: the void is the state
    # change, the deletion is what the person asked for.
    assert owner.audit_events.filter(event_type="transaction_voided").exists()


# ----------------------------------------------------------------------
# The page in front of it
# ----------------------------------------------------------------------


def test_the_delete_page_states_what_will_happen(owner: Any, account: Any) -> None:
    transaction, _ = posted_transaction(owner, account)
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("transaction-delete", kwargs={"pk": transaction.pk}))

    assert response.status_code == 200
    assert response.context["posted_entry_count"] == 2


def test_posting_the_delete_form_deletes_and_posting_without_confirming_does_not(
    owner: Any, account: Any
) -> None:
    transaction, _ = posted_transaction(owner, account)
    client = Client()
    client.force_login(owner)
    url = reverse("transaction-delete", kwargs={"pk": transaction.pk})

    unconfirmed = client.post(url, {})
    transaction.refresh_from_db()
    assert unconfirmed.status_code == 302
    assert transaction.status == CanonicalTransaction.Status.CONFIRMED

    confirmed = client.post(url, {"confirm": "yes", "reason": "Wrong card."})
    transaction.refresh_from_db()
    assert confirmed.status_code == 302
    assert transaction.status == CanonicalTransaction.Status.VOIDED


def test_the_delete_page_is_a_404_for_another_users_transaction(owner: Any, account: Any) -> None:
    transaction, _ = posted_transaction(owner, account)
    intruder = make_user(email="deletion-web-intruder@example.com", password=PASSWORD)
    client = Client()
    client.force_login(intruder)
    url = reverse("transaction-delete", kwargs={"pk": transaction.pk})

    assert client.get(url).status_code == 404
    assert client.post(url, {"confirm": "yes"}).status_code == 404
    transaction.refresh_from_db()
    assert transaction.status == CanonicalTransaction.Status.CONFIRMED


def test_deleting_a_transfer_through_the_browser_releases_and_relinks_its_sources(
    owner: Any, master_key: bytes
) -> None:
    data_key = get_user_data_key(user=owner, actor=owner, master_key=master_key)
    outgoing, incoming, source_account, destination_account = transfer_rows(owner, data_key)
    match = record_match(
        user=owner,
        left_observation_id=outgoing.pk,
        right_observation_id=incoming.pk,
        match_type=ReconciliationMatch.MatchType.INTERNAL_TRANSFER,
        score=85,
        features=("exact_amount", "same_date"),
        data_key=data_key,
    )
    source_ledger = make_ledger_accounts(
        owner, source_account, prefix="delete-transfer-src"
    ).account
    destination_ledger = make_ledger_accounts(
        owner, destination_account, prefix="delete-transfer-dst"
    ).account
    transfer = confirm_internal_transfer(
        match.pk,
        user=owner,
        data_key=data_key,
        ledger_accounts=PostingRuleAccounts(
            account=source_ledger,
            transfer_account=destination_ledger,
        ),
    )
    assert LedgerEntry.objects.filter(transaction=transfer).count() == 2

    client = Client()
    client.force_login(owner)
    delete_url = reverse("transaction-delete", kwargs={"pk": transfer.pk})
    assert client.get(delete_url).status_code == 200
    response = client.post(delete_url, {"confirm": "yes", "reason": "Corrected transfer."})

    assert response.status_code == 302
    transfer.refresh_from_db()
    match.refresh_from_db()
    for row in (outgoing, incoming):
        row.refresh_from_db()
        assert row.canonical_transaction_id is None
        assert row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
        assert row.source_document_id is not None

    assert transfer.status == CanonicalTransaction.Status.VOIDED
    assert match.status == ReconciliationMatch.Status.REJECTED
    assert LedgerEntry.objects.filter(transaction=transfer).count() == 4
    assert transaction_net_minor(transfer, data_key=data_key) == 0
    assert account_position(source_ledger, data_key=data_key) == 0
    assert account_position(destination_ledger, data_key=data_key) == 0
    totals = monthly_spending(owner, year=2026, month=8, data_key=data_key).totals("KRW")
    assert totals.transaction_count == 0
    assert totals.neutral_minor == 0

    unlinked = AuditEvent.objects.get(
        user=owner,
        event_type=AuditEvent.EventType.RECONCILIATION_MATCH_UNLINKED,
        object_id=match.pk,
    )
    assert unlinked.metadata["cause"] == "transaction_deleted"
    assert unlinked.metadata["canonical_transaction_id"] == str(transfer.pk)
    rejected_queue = client.get(reverse("match-queue"))
    assert all(row["match"].pk != match.pk for row in rejected_queue.context["rows"])

    # The released source rows can be linked again through the browser form.
    relinked = client.post(
        reverse("match-link"),
        data={
            "left_observation": str(outgoing.pk),
            "right_observation": str(incoming.pk),
            "match_type": ReconciliationMatch.MatchType.INTERNAL_TRANSFER,
        },
    )
    assert relinked.status_code == 302
    match.refresh_from_db()
    assert match.status == ReconciliationMatch.Status.PROPOSED
    assert match.resolution_revision == 1
    open_queue = client.get(reverse("match-queue"))
    assert any(row["match"].pk == match.pk for row in open_queue.context["rows"])

    reconfirmed = client.post(reverse("match-accept", kwargs={"pk": match.pk}))
    assert reconfirmed.status_code == 302
    match.refresh_from_db()
    outgoing.refresh_from_db()
    incoming.refresh_from_db()
    replacement = CanonicalTransaction.objects.get(pk=outgoing.canonical_transaction_id)
    assert match.status == ReconciliationMatch.Status.CONFIRMED
    assert replacement.pk != transfer.pk
    assert replacement.status == CanonicalTransaction.Status.DRAFT
    assert replacement.source_idempotency_key != transfer.source_idempotency_key
    assert incoming.canonical_transaction_id == replacement.pk

    # A retry of this review cycle returns the replacement and creates no third row.
    retry = client.post(reverse("match-accept", kwargs={"pk": match.pk}))
    assert retry.status_code == 302
    assert CanonicalTransaction.objects.filter(user=owner).count() == 2


@pytest.mark.parametrize(
    "match_type",
    [
        ReconciliationMatch.MatchType.DEBIT_CARD_BANK_MATCH,
        ReconciliationMatch.MatchType.CREDIT_CARD_PAYMENT,
    ],
)
def test_deleting_a_canonical_event_rejects_confirmed_future_match_types(
    owner: Any, master_key: bytes, match_type: str
) -> None:
    data_key = get_user_data_key(user=owner, actor=owner, master_key=master_key)
    outgoing, incoming, source_account, _ = transfer_rows(owner, data_key)
    transaction = accept_observation(
        outgoing.pk,
        user=owner,
        data_key=data_key,
        financial_account=source_account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    match = record_match(
        user=owner,
        left_observation_id=outgoing.pk,
        right_observation_id=incoming.pk,
        match_type=match_type,
        score=70,
        features=("exact_amount",),
        data_key=data_key,
    )
    confirm_match(match.pk, user=owner)

    delete_transaction(transaction.pk, user=owner, confirmed=True, data_key=data_key)

    transaction.refresh_from_db()
    match.refresh_from_db()
    outgoing.refresh_from_db()
    incoming.refresh_from_db()
    assert transaction.status == CanonicalTransaction.Status.VOIDED
    assert match.status == ReconciliationMatch.Status.REJECTED
    assert outgoing.canonical_transaction_id is None
    assert outgoing.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
    assert incoming.canonical_transaction_id is None
    assert incoming.review_status == ImportedObservation.ReviewStatus.UNREVIEWED


@pytest.mark.django_db(transaction=True)
def test_concurrent_delete_and_unlink_reverse_a_transfer_once_on_postgres(
    owner: Any, master_key: bytes
) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("The concurrent deletion/unlink assertion requires PostgreSQL row locks.")

    data_key = get_user_data_key(user=owner, actor=owner, master_key=master_key)
    outgoing, incoming, source_account, destination_account = transfer_rows(owner, data_key)
    match = record_match(
        user=owner,
        left_observation_id=outgoing.pk,
        right_observation_id=incoming.pk,
        match_type=ReconciliationMatch.MatchType.INTERNAL_TRANSFER,
        score=85,
        features=("exact_amount", "same_date"),
        data_key=data_key,
    )
    source_ledger = make_ledger_accounts(owner, source_account, prefix="race-delete-src").account
    destination_ledger = make_ledger_accounts(
        owner, destination_account, prefix="race-delete-dst"
    ).account
    transaction = confirm_internal_transfer(
        match.pk,
        user=owner,
        data_key=data_key,
        ledger_accounts=PostingRuleAccounts(
            account=source_ledger,
            transfer_account=destination_ledger,
        ),
    )
    barrier = threading.Barrier(2)

    def delete_directly() -> str:
        close_old_connections()
        try:
            worker_user = type(owner).objects.get(pk=owner.pk)
            barrier.wait(timeout=10)
            try:
                delete_transaction(
                    transaction.pk,
                    user=worker_user,
                    confirmed=True,
                    data_key=data_key,
                )
            except ConflictError:
                return "already handled"
            return "deleted"
        finally:
            close_old_connections()

    def unlink_relationship() -> str:
        close_old_connections()
        try:
            worker_user = type(owner).objects.get(pk=owner.pk)
            barrier.wait(timeout=10)
            try:
                unlink_match(match.pk, user=worker_user, data_key=data_key)
            except ConflictError:
                return "already handled"
            return "unlinked"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda action: action(), (delete_directly, unlink_relationship))
        )

    transaction.refresh_from_db()
    match.refresh_from_db()
    outgoing.refresh_from_db()
    incoming.refresh_from_db()
    assert sorted(results) == ["already handled", "deleted"] or sorted(results) == [
        "already handled",
        "unlinked",
    ]
    assert transaction.status == CanonicalTransaction.Status.VOIDED
    assert match.status == ReconciliationMatch.Status.REJECTED
    assert LedgerEntry.objects.filter(transaction=transaction).count() == 4
    assert transaction_net_minor(transaction, data_key=data_key) == 0
    assert outgoing.canonical_transaction_id is None
    assert incoming.canonical_transaction_id is None
    assert outgoing.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
    assert incoming.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
