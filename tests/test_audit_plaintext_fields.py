from datetime import date

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_transaction, make_user

pytestmark = pytest.mark.django_db


def test_plaintext_audit_detects_readable_values_without_printing_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    user = make_user(email="plaintext-audit@example.com")
    transaction = make_transaction(user, make_account(user), amount_encrypted="known-audit-marker")
    transaction.occurred_at = date(2026, 8, 20)
    CanonicalTransaction.objects.filter(pk=transaction.pk).update(
        amount_encrypted="known-audit-marker"
    )

    with pytest.raises(CommandError):
        call_command(
            "audit_plaintext_fields",
            marker="known-audit-marker",
            fail_on_findings=True,
        )

    output = capsys.readouterr()
    assert "transactions.CanonicalTransaction.amount_encrypted" in output.out
    assert "known-audit-marker" not in output.out
    assert "known-audit-marker" not in output.err
