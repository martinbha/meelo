from datetime import date
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command

from apps.reconciliation.models import ReconciliationMatch
from tests.factories import make_user
from tests.test_reconciliation_services import KEY, SEARCH_KEY, parsed, seed

pytestmark = pytest.mark.django_db


def test_batch_command_is_date_bounded_idempotent_and_reports_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner: Any = make_user(email="batch-owner@example.com")
    seed(owner, parsed(), parsed())
    command = "apps.reconciliation.management.commands.generate_reconciliation_candidates"
    monkeypatch.setattr(f"{command}.load_master_key", lambda: b"m" * 32)
    monkeypatch.setattr(f"{command}.get_user_data_key", lambda **kwargs: KEY)
    monkeypatch.setattr(f"{command}.get_user_search_key", lambda **kwargs: SEARCH_KEY)

    first = StringIO()
    call_command(
        "generate_reconciliation_candidates",
        email=owner.email,
        start="2026-08-01",
        end="2026-08-31",
        stdout=first,
    )
    second = StringIO()
    call_command(
        "generate_reconciliation_candidates",
        email=owner.email,
        start="2026-08-01",
        end="2026-08-31",
        stdout=second,
    )

    assert ReconciliationMatch.objects.filter(user=owner).count() == 1
    assert "examined=2 created=1 existing=0 skipped=1" in first.getvalue()
    assert "examined=2 created=0 existing=1 skipped=1" in second.getvalue()
    assert "4200" not in first.getvalue() + second.getvalue()


def test_scheduled_invocation_needs_no_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    owner: Any = make_user(email="scheduled-owner@example.com")
    seed(owner, parsed(), parsed())
    command = "apps.reconciliation.management.commands.generate_reconciliation_candidates"
    monkeypatch.setattr(f"{command}.load_master_key", lambda: b"m" * 32)
    monkeypatch.setattr(f"{command}.get_user_data_key", lambda **kwargs: KEY)
    monkeypatch.setattr(f"{command}.get_user_search_key", lambda **kwargs: SEARCH_KEY)
    monkeypatch.setattr(f"{command}.timezone.localdate", lambda: date(2026, 8, 31))

    output = StringIO()
    call_command("generate_reconciliation_candidates", stdout=output)

    assert ReconciliationMatch.objects.filter(user=owner).count() == 1
    assert "created=1" in output.getvalue()
