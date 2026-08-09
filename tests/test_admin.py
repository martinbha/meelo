from __future__ import annotations

from typing import Any

import pytest
from django.contrib import admin
from django.core.management import call_command

from apps.categorization.models import Category
from apps.financial_accounts.models import FinancialAccount
from apps.processing.models import ProcessingJob
from apps.transactions.models import CanonicalTransaction


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("admin-fixture@example.com", password="password")


@pytest.mark.django_db
def test_domain_fixture_command_is_idempotent(user: Any) -> None:
    call_command("load_domain_fixtures", email=user.email)
    call_command("load_domain_fixtures", email=user.email)

    assert Category.objects.filter(user=user).count() == 3
    assert FinancialAccount.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_domain_models_are_registered_with_safe_admin_lists() -> None:
    for model in (Category, FinancialAccount, ProcessingJob, CanonicalTransaction):
        assert model in admin.site._registry

    assert "amount_encrypted" not in admin.site._registry[CanonicalTransaction].list_display
    assert "name_encrypted" not in admin.site._registry[FinancialAccount].list_display
