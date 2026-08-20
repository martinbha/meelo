"""A single product-path regression from stored transaction to report/export."""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.utils import timezone

from apps.reports.models import TransactionExport
from apps.reports.services import create_export, delete_export, read_export
from apps.reports.spending import monthly_spending
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_transaction, make_user

pytestmark = pytest.mark.django_db


def test_transaction_reaches_report_and_export_with_matching_totals(
    settings: Any, tmp_path: Path
) -> None:
    settings.EXPORT_TMP_ROOT = str(tmp_path / "exports")
    user = make_user(email="end-to-end@example.com")
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    account = make_account(user)
    make_transaction(
        user,
        account,
        amount_encrypted="42900:KRW",
        merchant_encrypted="cafe",
        occurred_at=date(2026, 8, 20),
        status=CanonicalTransaction.Status.CONFIRMED,
    )

    report = monthly_spending(user, year=2026, month=8)
    assert report.totals("KRW").net_spending_minor == 42_900

    export = create_export(
        user=user,
        export_format=TransactionExport.Format.CSV,
        start=date(2026, 8, 1),
        end=date(2026, 8, 31),
    )
    record, payload = read_export(export.pk, user=user)
    assert record.row_count == 1
    assert b"42900" in payload
    assert b"cafe" in payload
    delete_export(record.pk, user=user)
    assert not Path(record.file_path).exists()
