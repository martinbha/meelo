"""Request-local, owner-scoped labels for model choice controls."""

from __future__ import annotations

from copy import copy
from typing import Any

from apps.core.crypto import InvalidCiphertextError, is_encrypted_value


def readable_encrypted_value(instance: Any, field: str, *, data_key: bytes) -> str | None:
    """Read one encrypted field without returning a broken envelope to HTML."""

    try:
        value = instance.read_field(field, key=data_key)
    except InvalidCiphertextError:
        return None
    if is_encrypted_value(value):
        # A field from a historical double-encryption bug can decrypt once to
        # another authenticated envelope. Do not display that envelope. A
        # plain user-entered string with a ``v1.`` prefix remains readable
        # unless it is itself a valid envelope for this exact field and row.
        parts = value.split(".")
        if len(parts) == 5 and parts[1].isdigit():
            probe = copy(instance)
            setattr(probe, field, value)
            try:
                probe.read_field(field, key=data_key)
            except InvalidCiphertextError:
                return value.strip() or None
            return None
    return value.strip() or None


def financial_account_label(account: Any, *, user: Any, data_key: bytes) -> str:
    """Name an owned account with type, institution, currency, and suffix."""

    if account.user_id != user.pk:
        return "Account unavailable"
    name = readable_encrypted_value(account, "name_encrypted", data_key=data_key)
    institution = readable_encrypted_value(account, "institution_encrypted", data_key=data_key)
    qualifiers = [account.get_account_type_display()]
    if institution:
        qualifiers.append(institution)
    elif account.institution_encrypted:
        qualifiers.append("Institution unavailable")
    if account.currency:
        qualifiers.append(account.currency)
    if account.identifier_last_four:
        qualifiers.append(f"ending {account.identifier_last_four}")
    return f"{name or 'Account name unavailable'} · {' · '.join(qualifiers)}"


def payment_instrument_label(instrument: Any, *, user: Any, data_key: bytes) -> str:
    """Name an owned card with issuer/type/suffix and its linked account."""

    if instrument.user_id != user.pk:
        return "Card unavailable"
    name = readable_encrypted_value(instrument, "name_encrypted", data_key=data_key)
    issuer = readable_encrypted_value(instrument, "issuer_encrypted", data_key=data_key)
    qualifiers = [instrument.get_instrument_type_display()]
    if issuer:
        qualifiers.append(issuer)
    elif instrument.issuer_encrypted:
        qualifiers.append("Issuer unavailable")
    if instrument.last_four:
        qualifiers.append(f"ending {instrument.last_four}")
    account = instrument.financial_account
    if account.user_id == user.pk:
        account_name = readable_encrypted_value(account, "name_encrypted", data_key=data_key)
        if account_name:
            qualifiers.append(f"account {account_name}")
    return f"{name or 'Card name unavailable'} · {' · '.join(qualifiers)}"


def category_label(category: Any, *, user: Any, data_key: bytes) -> str:
    """Show an owned category's hierarchy and type instead of its index."""

    if category.user_id != user.pk:
        return "Category unavailable"
    names: list[str] = []
    visited: set[Any] = set()
    current = category
    while current is not None:
        if current.user_id != user.pk or current.pk in visited:
            names.append("Category path unavailable")
            break
        visited.add(current.pk)
        name = readable_encrypted_value(current, "name_encrypted", data_key=data_key)
        names.append(name or "Category name unavailable")
        current = current.parent
    path = " › ".join(reversed(names))
    qualifiers = [category.get_category_type_display()]
    if category.is_system:
        qualifiers.append("System")
    return f"{path} · {' · '.join(qualifiers)}"
