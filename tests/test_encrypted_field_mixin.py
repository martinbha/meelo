"""One door in and out of every encrypted column (#158, specification 22.3).

The failure this guards against is not dramatic. A service forgets the owner
argument, or writes a plaintext merchant name into an ``_encrypted`` column, and
nothing complains — the column looks exactly the same either way until somebody
reads the database or tries to decrypt a value under an identity it was never
sealed with.

So the checks here are structural. Every encrypted column is declared, every
declaration matches the schema, nothing outside the crypto module reaches for
the primitives, and a ciphertext moved anywhere it does not belong refuses to
open.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps
from django.db import models

from apps.core.crypto import InvalidCiphertextError, is_encrypted_value
from apps.core.encrypted_fields import (
    EncryptedFieldsMixin,
    UndeclaredEncryptedFieldError,
    encrypted_column_names,
)
from apps.core.rotation import ENCRYPTED_MODELS
from apps.core.value_objects import Money
from apps.ledger.models import LedgerEntry
from apps.ledger.posting import Posting, post_balanced_transaction
from apps.transactions.lifecycle import transition_transaction_status
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_ledger_accounts, make_transaction, make_user

KEY = os.urandom(32)
REPOSITORY = Path(__file__).resolve().parent.parent

#: The primitives are the mixin's business and nobody else's. ``apps/core`` is
#: where the mixin and the rotation machinery live, so it is exempt.
PRIMITIVES = frozenset(
    {"encrypt_model_field", "encrypt_model_fields", "decrypt_model_field", "read_model_field"}
)


def encrypted_models() -> list[type[models.Model]]:
    return [
        model
        for model in apps.get_models()
        if model.__module__.startswith("apps.") and encrypted_column_names(model)
    ]


# ----------------------------------------------------------------------
# Every encrypted column is declared, and every declaration is real
# ----------------------------------------------------------------------


@pytest.mark.parametrize("model", encrypted_models(), ids=lambda model: model._meta.label)
def test_every_encrypted_column_goes_through_the_mixin(model: type[models.Model]) -> None:
    assert issubclass(model, EncryptedFieldsMixin), (
        f"{model._meta.label} has encrypted columns but does not use the shared mixin."
    )
    declared: set[str] = set(model.encrypted_fields)
    actual = set(encrypted_column_names(model))
    assert declared == actual, (
        f"{model._meta.label} declares {sorted(declared)} but its schema has {sorted(actual)}."
    )


def test_no_model_declares_a_column_it_does_not_have() -> None:
    for model in apps.get_models():
        if not issubclass(model, EncryptedFieldsMixin):
            continue
        columns = {field.attname for field in model._meta.concrete_fields}
        declared: set[str] = set(model.encrypted_fields)
        assert declared <= columns, model._meta.label


def test_rotation_covers_exactly_the_declared_fields() -> None:
    """A field the mixin encrypts but rotation skips would keep an old key alive."""

    rotated = {spec.label: set(spec.fields) for spec in ENCRYPTED_MODELS}
    for model in encrypted_models():
        label = model._meta.label
        assert label in rotated, f"{label} has encrypted columns that rotation never visits."
        assert rotated[label] == set(model.encrypted_fields), (  # type: ignore[attr-defined]
            f"Rotation and {label} disagree about which fields are encrypted."
        )


def test_no_service_reaches_for_the_primitives_directly() -> None:
    """A lint rule as a test: the shared path is only shared if it is the only path."""

    offenders: list[str] = []
    for path in sorted((REPOSITORY / "apps").rglob("*.py")):
        if path.parts[path.parts.index("apps") + 1] == "core":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in PRIMITIVES
            ):
                offenders.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}")
    assert offenders == [], (
        f"These call the encryption primitives instead of the mixin: {offenders}"
    )


# ----------------------------------------------------------------------
# The binding actually binds
# ----------------------------------------------------------------------


@pytest.mark.django_db
def test_a_value_moved_to_another_record_does_not_open() -> None:
    user = make_user(email="mixin-owner@example.com")
    account = make_account(user)
    first = make_transaction(user, account)
    second = make_transaction(user, account, occurred_at=first.occurred_at)

    first.encrypt_fields({"merchant_encrypted": "스타벅스"}, key=KEY)
    second.merchant_encrypted = first.merchant_encrypted

    assert first.decrypt_field("merchant_encrypted", key=KEY) == "스타벅스"
    with pytest.raises(InvalidCiphertextError):
        second.decrypt_field("merchant_encrypted", key=KEY)


@pytest.mark.django_db
def test_a_value_moved_to_another_field_does_not_open() -> None:
    user = make_user(email="mixin-field@example.com")
    account = make_account(user)
    transaction = make_transaction(user, account)

    transaction.encrypt_fields({"merchant_encrypted": "스타벅스"}, key=KEY)
    transaction.notes_encrypted = transaction.merchant_encrypted

    with pytest.raises(InvalidCiphertextError):
        transaction.decrypt_field("notes_encrypted", key=KEY)


@pytest.mark.django_db
def test_a_value_moved_to_another_users_record_does_not_open() -> None:
    owner = make_user(email="mixin-a@example.com")
    stranger = make_user(email="mixin-b@example.com")
    mine = make_transaction(owner, make_account(owner))
    theirs = make_transaction(stranger, make_account(stranger, name_blind_index="mixin-b"))

    mine.encrypt_fields({"merchant_encrypted": "스타벅스"}, key=KEY)
    # Same key, same field, same position in the row — only the owner differs.
    theirs.pk = mine.pk
    theirs.merchant_encrypted = mine.merchant_encrypted

    with pytest.raises(InvalidCiphertextError):
        theirs.decrypt_field("merchant_encrypted", key=KEY)


@pytest.mark.django_db
def test_a_ledger_entry_borrows_its_owner_from_its_transaction() -> None:
    """The entry has no owner column, and the ciphertext is still bound to one."""

    user = make_user(email="mixin-ledger@example.com")
    account = make_account(user)
    transaction = make_transaction(user, account, amount_encrypted="4200:KRW")
    transaction = transition_transaction_status(
        transaction.pk, user=user, status=CanonicalTransaction.Status.CONFIRMED
    )
    accounts = make_ledger_accounts(user, account, prefix="mixin")
    entries = post_balanced_transaction(
        transaction,
        [
            Posting(accounts.offset, LedgerEntry.EntryType.DEBIT, Money(4_200, "KRW")),
            Posting(accounts.account, LedgerEntry.EntryType.CREDIT, Money(4_200, "KRW")),
        ],
        data_key=KEY,
    )

    entry = entries[0]
    assert entry.encryption_owner_id == user.pk
    assert is_encrypted_value(entry.amount_encrypted)
    assert entry.read_field("amount_encrypted", key=KEY) == "4200:KRW"


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


@pytest.mark.django_db
def test_writing_an_undeclared_field_is_refused() -> None:
    """A typo'd field name would otherwise write a ciphertext nobody reads."""

    user = make_user(email="mixin-undeclared@example.com")
    transaction = make_transaction(user, make_account(user))

    with pytest.raises(UndeclaredEncryptedFieldError, match="merchant_ecnrypted"):
        transaction.encrypt_fields({"merchant_ecnrypted": "typo"}, key=KEY)
    with pytest.raises(UndeclaredEncryptedFieldError):
        transaction.read_field("currency")


@pytest.mark.django_db
def test_an_empty_value_is_stored_empty_rather_than_encrypted() -> None:
    """Otherwise "no note" and "a note nobody can read" look identical."""

    user = make_user(email="mixin-empty@example.com")
    transaction = make_transaction(user, make_account(user))

    transaction.encrypt_fields({"notes_encrypted": ""}, key=KEY)

    assert transaction.notes_encrypted == ""
    assert transaction.read_field("notes_encrypted", key=KEY) == ""


@pytest.mark.django_db
def test_plaintext_in_an_encrypted_column_is_reported() -> None:
    user = make_user(email="mixin-plaintext@example.com")
    transaction = make_transaction(user, make_account(user))
    transaction.merchant_encrypted = "스타벅스"

    assert transaction.plaintext_fields() == ("merchant_encrypted", "amount_encrypted")
    assert not transaction.is_fully_encrypted

    transaction.encrypt_fields(
        {"merchant_encrypted": "스타벅스", "amount_encrypted": "100:KRW"}, key=KEY
    )
    assert transaction.is_fully_encrypted


@pytest.mark.django_db
def test_reading_an_encrypted_value_without_a_key_raises_rather_than_returning_it() -> None:
    """A caller handed ciphertext would display it, index it, or add it up."""

    from apps.core.crypto import EncryptionError

    user = make_user(email="mixin-nokey@example.com")
    transaction = make_transaction(user, make_account(user))
    transaction.encrypt_fields({"merchant_encrypted": "스타벅스"}, key=KEY)

    with pytest.raises(EncryptionError):
        transaction.read_field("merchant_encrypted")


@pytest.mark.django_db
def test_the_real_creation_path_leaves_nothing_readable() -> None:
    """The acceptance criterion, checked through the service rather than the mixin."""

    from datetime import date

    from apps.transactions.services import create_manual_transaction

    user = make_user(email="mixin-service@example.com")
    account = make_account(user)

    transaction: Any = create_manual_transaction(
        user=user,
        occurred_at=date(2026, 8, 15),
        amount_minor=42_900,
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=account,
        merchant="스타벅스 강남점",
        counterparty="김대성",
        notes="a private note",
        data_key=KEY,
    )

    stored = CanonicalTransaction.objects.get(pk=transaction.pk)
    assert stored.is_fully_encrypted
    assert stored.plaintext_fields() == ()
