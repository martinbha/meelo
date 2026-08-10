from uuid import uuid4

import pytest

from apps.core.crypto import (
    FieldContext,
    InvalidCiphertextError,
    decrypt_model_field,
    decrypt_value,
    encrypt_model_field,
    encrypt_value,
)
from apps.financial_accounts.models import FinancialAccount


def context(**overrides: str) -> FieldContext:
    values = {
        "model": "transactions.canonicaltransaction",
        "record_id": str(uuid4()),
        "field": "merchant_encrypted",
        "user_id": "42",
    }
    values.update(overrides)
    return FieldContext(**values)


def test_aes_gcm_round_trip_uses_unique_nonces() -> None:
    key = bytes(range(32))
    field_context = context()

    first = encrypt_value("Private Cafe", key=key, context=field_context, key_version=3)
    second = encrypt_value("Private Cafe", key=key, context=field_context, key_version=3)

    assert first != second
    assert first.startswith("v1.3.")
    assert decrypt_value(first, key=key, context=field_context) == "Private Cafe"
    assert decrypt_value(second, key=key, context=field_context) == "Private Cafe"


def test_modified_ciphertext_and_context_fail_authentication() -> None:
    key = bytes(range(32))
    field_context = context()
    encrypted = encrypt_value("42900", key=key, context=field_context, key_version=1)
    parts = encrypted.split(".")
    parts[3] = ("A" if parts[3][0] != "A" else "B") + parts[3][1:]

    with pytest.raises(InvalidCiphertextError):
        decrypt_value(".".join(parts), key=key, context=field_context)
    with pytest.raises(InvalidCiphertextError):
        decrypt_value(encrypted, key=key, context=context(field="notes_encrypted"))

    version_parts = encrypted.split(".")
    version_parts[1] = "2"
    with pytest.raises(InvalidCiphertextError):
        decrypt_value(".".join(version_parts), key=key, context=field_context)


def test_django_model_helpers_bind_model_record_field_and_user() -> None:
    key = bytes(reversed(range(32)))
    account = FinancialAccount(
        id=uuid4(),
        user_id=7,
        name_encrypted="",
        name_blind_index="name-index",
        institution_encrypted="",
        institution_blind_index="institution-index",
        account_type=FinancialAccount.AccountType.CHECKING,
    )

    account.name_encrypted = encrypt_model_field(
        account,
        "name_encrypted",
        "Private checking",
        key=key,
        key_version=1,
    )

    assert decrypt_model_field(account, "name_encrypted", key=key) == "Private checking"
