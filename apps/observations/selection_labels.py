from __future__ import annotations

from contextlib import suppress
from typing import Any

from apps.core.selection_labels import readable_encrypted_value
from apps.core.value_objects import Money

from .models import ImportedObservation


def observation_label(observation: ImportedObservation, *, user: Any, data_key: bytes) -> str:
    """Identify an owned observation by date, merchant, amount, and row."""

    if observation.user_id != user.pk:
        return "Row unavailable"
    date_label = observation.occurred_at.isoformat() if observation.occurred_at else "Undated"
    row_label = f"Row {observation.row_index + 1}"
    merchant_value = readable_encrypted_value(
        observation, "merchant_raw_encrypted", data_key=data_key
    )
    merchant = " ".join(merchant_value.split()) if merchant_value else "Merchant unavailable"
    amount_value = readable_encrypted_value(observation, "amount_encrypted", data_key=data_key)
    amount_label = "Amount unavailable"
    if amount_value:
        minor, separator, currency = amount_value.partition(":")
        if separator:
            with suppress(TypeError, ValueError):
                amount_label = Money(int(minor), currency).format()
    return (
        f"{date_label} · {merchant} · {amount_label} · "
        f"{observation.get_direction_display()} · {row_label}"
    )
