"""AES-256-GCM field encryption (#91, specification 22.3).

Three claims, each tested here rather than asserted in a docstring:

- a modified ciphertext fails authentication instead of decrypting to something,
- a nonce is never reused with the same key,
- and no readable financial value reaches the database on the production paths.

The last one is the reason the other two matter. An envelope format nobody uses on
the write path protects nothing.
"""

from __future__ import annotations

import base64
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.crypto import (
    FORMAT_VERSION,
    NONCE_SIZE,
    EncryptionError,
    FieldContext,
    InvalidCiphertextError,
    decrypt_model_field,
    decrypt_value,
    encrypt_model_field,
    encrypt_model_fields,
    encrypt_value,
    is_encrypted_value,
    model_field_context,
    read_model_field,
)
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.ledger.models import LedgerEntry
from apps.ledger.posting import entry_amount
from apps.observations.review import accept_observation
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import read_money, store_money
from apps.transactions.services import create_manual_transaction, update_manual_transaction
from tests.factories import (
    make_account,
    make_document,
    make_ledger_accounts,
    make_ocr_run,
    make_user,
)
from tests.plaintext import stored_text

pytestmark = pytest.mark.django_db

KEY = os.urandom(32)
MERCHANT = "스타벅스 강남점"


def context(**overrides: Any) -> FieldContext:
    values: dict[str, Any] = {
        "model": "transactions.canonicaltransaction",
        "record_id": "row-1",
        "field": "amount_encrypted",
        "user_id": "1",
    }
    values.update(overrides)
    return FieldContext(**values)


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


def test_the_envelope_carries_its_version_nonce_ciphertext_and_tag() -> None:
    envelope = encrypt_value("4200:KRW", key=KEY, context=context(), key_version=3)

    version, key_version, nonce, ciphertext, tag = envelope.split(".")

    assert version == FORMAT_VERSION
    assert key_version == "3"
    assert len(base64.urlsafe_b64decode(nonce)) == NONCE_SIZE
    assert base64.urlsafe_b64decode(ciphertext)
    assert len(base64.urlsafe_b64decode(tag)) == 16


def test_a_value_round_trips() -> None:
    envelope = encrypt_value("4200:KRW", key=KEY, context=context(), key_version=1)

    assert decrypt_value(envelope, key=KEY, context=context()) == "4200:KRW"


def test_the_ciphertext_hides_the_value() -> None:
    envelope = encrypt_value(MERCHANT, key=KEY, context=context(), key_version=1)

    assert MERCHANT not in envelope
    assert is_encrypted_value(envelope)


# ---------------------------------------------------------------------------
# Modified ciphertext fails authentication
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("part", [2, 3, 4])
def test_altering_any_part_of_the_envelope_fails_authentication(part: int) -> None:
    """Not a wrong value: a refusal."""

    pieces = encrypt_value("4200:KRW", key=KEY, context=context(), key_version=1).split(".")
    raw = bytearray(base64.urlsafe_b64decode(pieces[part]))
    raw[-1] ^= 0xFF
    pieces[part] = base64.urlsafe_b64encode(bytes(raw)).decode()

    with pytest.raises(InvalidCiphertextError):
        decrypt_value(".".join(pieces), key=KEY, context=context())


def test_the_wrong_key_fails_authentication() -> None:
    envelope = encrypt_value("4200:KRW", key=KEY, context=context(), key_version=1)

    with pytest.raises(InvalidCiphertextError):
        decrypt_value(envelope, key=os.urandom(32), context=context())


@pytest.mark.parametrize(
    "changed",
    [
        {"model": "ledger.ledgerentry"},
        {"record_id": "row-2"},
        {"field": "notes_encrypted"},
        {"user_id": "2"},
    ],
)
def test_a_value_cannot_be_moved_to_another_context(changed: dict[str, Any]) -> None:
    """The associated data binds a value to its model, row, field, and owner.

    Without this, a ciphertext could be copied from one row to another — or from
    one user to another — and would decrypt into a value that was never theirs.
    """

    envelope = encrypt_value("4200:KRW", key=KEY, context=context(), key_version=1)

    with pytest.raises(InvalidCiphertextError):
        decrypt_value(envelope, key=KEY, context=context(**changed))


def test_a_value_cannot_be_read_under_a_different_key_version() -> None:
    """The version is authenticated, so a downgrade is not readable."""

    envelope = encrypt_value("4200:KRW", key=KEY, context=context(), key_version=2)
    downgraded = envelope.replace("v1.2.", "v1.1.", 1)

    with pytest.raises(InvalidCiphertextError):
        decrypt_value(downgraded, key=KEY, context=context())


def test_a_malformed_envelope_is_refused_rather_than_guessed_at() -> None:
    for broken in ("", "not-an-envelope", "v1.1.only-three-parts", "v1.x.a.b.c"):
        with pytest.raises(EncryptionError):
            decrypt_value(broken, key=KEY, context=context())


def test_a_key_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(EncryptionError):
        encrypt_value("4200:KRW", key=b"short", context=context(), key_version=1)


def test_a_key_version_must_be_positive() -> None:
    with pytest.raises(EncryptionError):
        encrypt_value("4200:KRW", key=KEY, context=context(), key_version=0)


# ---------------------------------------------------------------------------
# Nonces are never reused with the same key
# ---------------------------------------------------------------------------


def test_every_encryption_draws_a_fresh_nonce() -> None:
    """A repeated nonce under one key does not degrade GCM, it breaks it."""

    nonces = {
        encrypt_value("4200:KRW", key=KEY, context=context(), key_version=1).split(".")[2]
        for _ in range(2_000)
    }

    assert len(nonces) == 2_000


def test_encrypting_the_same_value_twice_gives_different_ciphertext() -> None:
    """Otherwise equal amounts would be visible as equal in the database."""

    first = encrypt_value("4200:KRW", key=KEY, context=context(), key_version=1)
    second = encrypt_value("4200:KRW", key=KEY, context=context(), key_version=1)

    assert first != second
    assert decrypt_value(first, key=KEY, context=context()) == decrypt_value(
        second, key=KEY, context=context()
    )


# ---------------------------------------------------------------------------
# The model helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def owner() -> Any:
    return make_user(email="encryption-owner@example.com")


@pytest.fixture
def account(owner: Any) -> Any:
    return make_account(owner, name_blind_index="encryption-account")


def test_several_fields_encrypt_together(owner: Any, account: Any) -> None:
    transaction = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="4200:KRW",
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    encrypt_model_fields(
        transaction,
        {"merchant_encrypted": MERCHANT, "notes_encrypted": "", "counterparty_encrypted": "김대성"},
        key=KEY,
        key_version=1,
    )

    assert decrypt_model_field(transaction, "merchant_encrypted", key=KEY) == MERCHANT
    assert decrypt_model_field(transaction, "counterparty_encrypted", key=KEY) == "김대성"
    # An empty value is left alone: storing a ciphertext would make the absence
    # of a value look like a value.
    assert transaction.notes_encrypted == ""


def test_a_record_with_no_identity_cannot_be_encrypted(owner: Any, account: Any) -> None:
    """The associated data needs the row it is binding to."""

    unsaved = CanonicalTransaction(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    unsaved.pk = None

    with pytest.raises(EncryptionError):
        encrypt_model_field(unsaved, "merchant_encrypted", MERCHANT, key=KEY, key_version=1)


def test_an_owner_can_be_supplied_for_a_record_that_has_none(owner: Any, account: Any) -> None:
    """A ledger entry belongs to whoever owns its transaction."""

    transaction = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="4200:KRW",
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    entry = LedgerEntry(transaction=transaction, entry_type=LedgerEntry.EntryType.DEBIT)

    built = model_field_context(entry, "amount_encrypted", user_id=owner.pk)

    assert built.user_id == str(owner.pk)
    assert built.model == "ledger.ledgerentry"
    # The entry itself carries no owner, which is why one has to be supplied.
    assert getattr(entry, "user_id", None) is None


def test_reading_an_unencrypted_field_returns_it_unchanged(owner: Any, account: Any) -> None:
    """Rows written before encryption reached this model are still readable."""

    transaction = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="4200:KRW",
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        merchant_encrypted="plain text merchant",
    )

    assert read_model_field(transaction, "merchant_encrypted") == "plain text merchant"
    assert read_money(transaction, "amount_encrypted").amount_minor == 4200


def test_reading_an_encrypted_field_without_a_key_raises(owner: Any, account: Any) -> None:
    """Returning the envelope would let a caller display or index ciphertext."""

    transaction = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="4200:KRW",
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    encrypt_model_fields(transaction, {"merchant_encrypted": MERCHANT}, key=KEY, key_version=1)

    with pytest.raises(EncryptionError):
        read_model_field(transaction, "merchant_encrypted")


def test_storing_money_encrypts_the_whole_encoding(owner: Any, account: Any) -> None:
    """The currency lives inside the ciphertext, so it cannot be edited alone."""

    from apps.core.value_objects import Money

    transaction = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="1:KRW",
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    store_money(transaction, "amount_encrypted", Money(4200, "KRW"), data_key=KEY)

    assert "4200" not in transaction.amount_encrypted
    assert "KRW" not in transaction.amount_encrypted
    assert read_money(transaction, "amount_encrypted", data_key=KEY).amount_minor == 4200


# ---------------------------------------------------------------------------
# No readable financial value reaches the database
# ---------------------------------------------------------------------------


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    return key


@pytest.fixture
def web_owner(master_key: bytes) -> Any:
    user = make_user(email="encryption-web@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


def test_a_manually_entered_transaction_stores_nothing_readable(owner: Any, account: Any) -> None:
    transaction = create_manual_transaction(
        user=owner,
        occurred_at=date(2026, 8, 15),
        amount_minor=42_900,
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=account,
        merchant=MERCHANT,
        counterparty="김대성",
        notes="a private note",
        data_key=KEY,
    )

    stored = CanonicalTransaction.objects.get(pk=transaction.pk)
    for field in (
        "amount_encrypted",
        "merchant_encrypted",
        "counterparty_encrypted",
        "notes_encrypted",
    ):
        assert is_encrypted_value(getattr(stored, field)), field
    assert MERCHANT not in stored_text(stored)
    assert "42900" not in stored_text(stored)
    assert "a private note" not in stored_text(stored)


def test_updating_a_transaction_leaves_nothing_readable(owner: Any, account: Any) -> None:
    transaction = create_manual_transaction(
        user=owner,
        occurred_at=date(2026, 8, 15),
        amount_minor=1_000,
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=account,
        data_key=KEY,
    )

    update_manual_transaction(
        transaction.pk,
        user=owner,
        occurred_at=date(2026, 8, 16),
        amount_minor=42_900,
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=account,
        merchant=MERCHANT,
        data_key=KEY,
    )

    stored = CanonicalTransaction.objects.get(pk=transaction.pk)
    assert read_money(stored, "amount_encrypted", data_key=KEY).amount_minor == 42_900
    assert MERCHANT not in stored_text(stored)
    assert "42900" not in stored_text(stored)


def parsed(**overrides: Any) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 8, 15),
        "amount": Decimal("42900"),
        "currency": "KRW",
        "direction": TransactionDirection.DEBIT,
        "merchant": MERCHANT,
        "confidence_factors": {"token_confidence": 0.95, "amount_confidence": 0.95},
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def test_card_payment_import_sets_the_settlement_type(owner: Any) -> None:
    document = make_document(owner, file_sha256="d" * 64)
    run = make_ocr_run(owner, document)

    result = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("hyundai_card", "1.0"),
            ParserSupport(0.95, "credit_card_payment", ()),
            (parsed(is_settlement=True, parser_name="hyundai_card"),),
        ),
        data_key=KEY,
        key_version=1,
    )

    assert result.observations[0].transaction_type_guess == "credit_card_payment"


def test_accepting_an_observation_stores_nothing_readable(owner: Any, account: Any) -> None:
    """The observation held these encrypted; acceptance must not undo that."""

    document = make_document(owner, file_sha256="e" * 64)
    run = make_ocr_run(owner, document)
    row = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (parsed(),),
        ),
        data_key=KEY,
        key_version=1,
    ).observations[0]

    transaction = accept_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    stored = CanonicalTransaction.objects.get(pk=transaction.pk)
    assert is_encrypted_value(stored.amount_encrypted)
    assert is_encrypted_value(stored.merchant_encrypted)
    assert MERCHANT not in stored_text(stored)
    assert "42900" not in stored_text(stored)
    assert read_money(stored, "amount_encrypted", data_key=KEY).amount_minor == 42_900


def test_ledger_entries_store_nothing_readable(owner: Any, account: Any) -> None:
    """An entry amount is a second copy of money already encrypted."""

    context_accounts = make_ledger_accounts(owner, account, prefix="enc")
    document = make_document(owner, file_sha256="f" * 64)
    run = make_ocr_run(owner, document)
    row = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (parsed(),),
        ),
        data_key=KEY,
        key_version=1,
    ).observations[0]

    transaction = accept_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        ledger_accounts=context_accounts,
    )

    entries = list(LedgerEntry.objects.filter(transaction=transaction))
    assert len(entries) == 2
    for entry in entries:
        assert is_encrypted_value(entry.amount_encrypted)
        assert "42900" not in entry.amount_encrypted
        assert entry_amount(entry, data_key=KEY).amount_minor == 42_900


def test_the_manual_entry_page_stores_nothing_readable(web_owner: Any, master_key: bytes) -> None:
    """The production path, not a service call with a key handed to it."""

    account = make_account(web_owner, name_blind_index="encryption-web-account")
    client = Client()
    client.force_login(web_owner)

    response = client.post(
        reverse("transaction-new"),
        data={
            "occurred_at": "2026-08-15",
            "amount": "429.00",
            "currency": "KRW",
            "transaction_type": CanonicalTransaction.TransactionType.PURCHASE,
            "financial_account": str(account.pk),
            "merchant": MERCHANT,
        },
    )

    assert response.status_code in {200, 302}
    stored = CanonicalTransaction.objects.filter(user=web_owner).first()
    assert stored is not None
    data_key = get_user_data_key(user=web_owner, actor=web_owner, master_key=master_key)
    assert is_encrypted_value(stored.amount_encrypted), stored.amount_encrypted
    assert is_encrypted_value(stored.merchant_encrypted)
    assert MERCHANT not in stored_text(stored)
    assert read_model_field(stored, "merchant_encrypted", key=data_key) == MERCHANT


def test_the_reports_and_the_ledger_read_an_amount_the_same_way(owner: Any, account: Any) -> None:
    """One rule for reading money, not one per caller."""

    from apps.core.value_objects import Money
    from apps.reports.amounts import transaction_amount

    transaction = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="1:KRW",
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    store_money(transaction, "amount_encrypted", Money(42_900, "KRW"), data_key=KEY)

    assert transaction_amount(transaction, data_key=KEY) == read_money(
        transaction, "amount_encrypted", data_key=KEY
    )
    # And both refuse an encrypted row with no key rather than answering zero.
    with pytest.raises(EncryptionError):
        transaction_amount(transaction)
