"""Nothing readable survives in an encrypted column (#163, specification 22.3, 22.5).

Two halves, and the second is the one that matters.

The first is the migration: rows written before their column was encrypted are
sealed in bounded, resumable batches. The second is that no *new* plaintext
appears — and that half is easy to get wrong, because the write paths accept a
missing key and store the value in clear, which is a convenience for fixtures
and a hole in production.

The test settings turn the requirement off so fixtures do not all have to carry
a key. So these tests turn it back on and drive the real services through it.
An off switch nothing tests is an off switch that turns out to have been on.
"""

from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connection

from apps.core.crypto import is_encrypted_value
from apps.core.encrypted_fields import (
    EncryptedFieldsMixin,
    PlaintextWriteError,
    encryption_required,
)
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.core.management.commands.encrypt_plaintext_fields import (
    SealableModel,
    seal_user,
    sealable_models,
)
from apps.core.value_objects import Money
from apps.ledger.models import LedgerEntry
from apps.ledger.posting import Posting, post_balanced_transaction
from apps.transactions.lifecycle import transition_transaction_status
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import store_money
from apps.transactions.services import create_manual_transaction
from tests.factories import make_account, make_ledger_accounts, make_transaction, make_user

pytestmark = pytest.mark.django_db

#: Restricting a count-sensitive test to one model, so the figure means what it
#: says. A whole-user run also seals the account's own name and institution, and
#: an assertion that quietly included those would break the next time a fixture
#: gained a field.
ONLY_TRANSACTIONS = [SealableModel("transactions.CanonicalTransaction")]

MERCHANT = "스타벅스 강남점"
COUNTERPARTY = "김대성"
NOTE = "a private note"


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="sealing-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


@pytest.fixture
def strict(settings: Any) -> Any:
    """The production configuration, which the suite does not run under."""

    settings.FIELD_ENCRYPTION_REQUIRED = True
    return settings


# ----------------------------------------------------------------------
# No new plaintext, under the production setting
# ----------------------------------------------------------------------


def test_the_requirement_is_on_by_default_and_off_only_in_tests(settings: Any) -> None:
    assert encryption_required() is False, "The test settings should relax it."
    settings.FIELD_ENCRYPTION_REQUIRED = True
    assert encryption_required() is True


def test_a_transaction_written_with_no_key_is_refused(strict: Any, owner: Any) -> None:
    account = make_account(owner)

    with pytest.raises(PlaintextWriteError):
        create_manual_transaction(
            user=owner,
            occurred_at=date(2026, 8, 15),
            amount_minor=42_900,
            currency="KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
            financial_account=account,
            merchant=MERCHANT,
        )

    assert not CanonicalTransaction.objects.exists()


def test_a_ledger_posting_with_no_key_is_refused(strict: Any, owner: Any) -> None:
    account = make_account(owner)
    transaction = make_transaction(owner, account, amount_encrypted="4200:KRW")
    transaction = transition_transaction_status(
        transaction.pk, user=owner, status=CanonicalTransaction.Status.CONFIRMED
    )
    accounts = make_ledger_accounts(owner, account, prefix="sealing")

    with pytest.raises(PlaintextWriteError):
        post_balanced_transaction(
            transaction,
            [
                Posting(accounts.offset, LedgerEntry.EntryType.DEBIT, Money(4_200, "KRW")),
                Posting(accounts.account, LedgerEntry.EntryType.CREDIT, Money(4_200, "KRW")),
            ],
        )

    assert not LedgerEntry.objects.exists()


def test_store_money_with_no_key_is_refused(strict: Any, owner: Any) -> None:
    transaction = make_transaction(owner, make_account(owner))

    with pytest.raises(PlaintextWriteError):
        store_money(transaction, "amount_encrypted", Money(100, "KRW"))


def test_the_real_creation_path_leaves_nothing_readable(
    strict: Any, owner: Any, data_key: bytes
) -> None:
    """The acceptance criterion, through the service rather than the mixin."""

    transaction = create_manual_transaction(
        user=owner,
        occurred_at=date(2026, 8, 15),
        amount_minor=42_900,
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=make_account(owner),
        merchant=MERCHANT,
        counterparty=COUNTERPARTY,
        notes=NOTE,
        data_key=data_key,
    )

    stored = CanonicalTransaction.objects.get(pk=transaction.pk)
    assert stored.plaintext_fields() == ()
    for value in (MERCHANT, COUNTERPARTY, NOTE, "42900"):
        assert value not in _dump_row(stored)


# ----------------------------------------------------------------------
# Sealing the rows that predate the column
# ----------------------------------------------------------------------


def _dump_row(instance: Any) -> str:
    """Every text column of one row, as a database dump would show it."""

    with connection.cursor() as cursor:
        table = instance._meta.db_table
        cursor.execute(f"SELECT * FROM {table} WHERE id = %s", [str(instance.pk)])
        return " ".join(str(value) for value in (cursor.fetchone() or ()))


def legacy_rows(owner: Any, count: int) -> list[CanonicalTransaction]:
    """Rows as they look when written before the column was encrypted."""

    account = make_account(owner, name_blind_index="sealing-legacy")
    return [
        make_transaction(
            owner,
            account,
            amount_encrypted=f"{1000 + index}:KRW",
            merchant_encrypted=MERCHANT,
            occurred_at=date(2026, 8, 1 + index),
        )
        for index in range(count)
    ]


def test_sealing_encrypts_every_readable_value(owner: Any, data_key: bytes) -> None:
    rows = legacy_rows(owner, 4)
    assert all(not is_encrypted_value(row.merchant_encrypted) for row in rows)

    report = seal_user(
        user=owner, data_key=data_key, key_version=1, batch_size=2, models=ONLY_TRANSACTIONS
    )

    assert report.is_clean
    for row in rows:
        row.refresh_from_db()
        assert row.plaintext_fields() == ()
        assert MERCHANT not in _dump_row(row)
    assert report.values_sealed == 2 * len(rows)


def test_sealing_is_idempotent(owner: Any, data_key: bytes) -> None:
    legacy_rows(owner, 3)
    seal_user(user=owner, data_key=data_key, key_version=1, batch_size=2)

    again = seal_user(user=owner, data_key=data_key, key_version=1, batch_size=2)

    assert again.values_sealed == 0
    assert again.rows_examined > 0


def test_an_interrupted_run_seals_only_what_is_left(owner: Any, data_key: bytes) -> None:
    rows = legacy_rows(owner, 6)
    first_three = sorted(rows, key=lambda row: row.pk)[:3]
    for row in first_three:
        row.encrypt_fields(
            {"merchant_encrypted": MERCHANT, "amount_encrypted": row.amount_encrypted},
            key=data_key,
            key_version=1,
        )
        row.save(update_fields=["merchant_encrypted", "amount_encrypted"])

    report = seal_user(
        user=owner, data_key=data_key, key_version=1, batch_size=2, models=ONLY_TRANSACTIONS
    )

    assert report.values_sealed == 2 * 3
    for row in rows:
        row.refresh_from_db()
        assert row.plaintext_fields() == ()


def test_a_dry_run_changes_nothing(owner: Any, data_key: bytes) -> None:
    rows = legacy_rows(owner, 2)

    report = seal_user(
        user=owner, data_key=data_key, key_version=1, dry_run=True, models=ONLY_TRANSACTIONS
    )

    assert report.values_sealed == 4
    for row in rows:
        row.refresh_from_db()
        assert row.merchant_encrypted == MERCHANT


def test_the_application_still_reads_every_sealed_value(owner: Any, data_key: bytes) -> None:
    """Sealing that broke reading would be worse than the plaintext it removed."""

    from apps.reports.spending import monthly_spending

    rows = legacy_rows(owner, 3)
    before = monthly_spending(owner, year=2026, month=8, data_key=data_key)

    seal_user(user=owner, data_key=data_key, key_version=1)

    after = monthly_spending(owner, year=2026, month=8, data_key=data_key)
    assert after.totals("KRW") == before.totals("KRW")
    for row in rows:
        row.refresh_from_db()
        assert row.read_field("merchant_encrypted", key=data_key) == MERCHANT


def test_sealing_never_reaches_another_users_rows(
    owner: Any, data_key: bytes, master_key: bytes
) -> None:
    stranger = make_user(email="sealing-stranger@example.com")
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)
    theirs = make_transaction(
        stranger,
        make_account(stranger, name_blind_index="sealing-theirs"),
        merchant_encrypted=MERCHANT,
    )
    legacy_rows(owner, 2)

    seal_user(user=owner, data_key=data_key, key_version=1)

    theirs.refresh_from_db()
    assert theirs.merchant_encrypted == MERCHANT


def test_the_command_runs_and_warns_that_it_is_one_way(
    owner: Any, data_key: bytes, capsys: Any
) -> None:
    legacy_rows(owner, 2)

    call_command("encrypt_plaintext_fields", email=owner.email)

    output = capsys.readouterr().out
    assert "one-way" in output.lower()
    assert owner.email in output


# ----------------------------------------------------------------------
# Coverage of the model list
# ----------------------------------------------------------------------


def test_every_model_with_encrypted_columns_is_sealable() -> None:
    """Discovered from the declarations, so a new column cannot be missed."""

    from django.apps import apps

    declared = {
        model._meta.label
        for model in apps.get_models()
        if model.__module__.startswith("apps.")
        and issubclass(model, EncryptedFieldsMixin)
        and model.encrypted_fields
    }

    assert {spec.label for spec in sealable_models()} == declared


def test_a_record_with_no_owner_column_is_reached_through_its_parent() -> None:
    entry = next(spec for spec in sealable_models() if spec.label == "ledger.LedgerEntry")

    assert entry.owner_filter == "transaction__user_id"
