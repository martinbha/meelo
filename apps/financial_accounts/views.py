"""Listing the accounts a person has told the system about.

These are the pages specification section 24 names at ``/accounts/``. They read
and nothing more: creating, editing, and deactivating accounts is #183, and the
richer detail page with balances and recent activity is #182. What is here now
is the part that can be built honestly from what already exists — the account
names, decrypted for their owner, and the balances the ledger derives.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.generic import View

from apps.core.key_scope import request_data_key
from apps.core.ownership import get_owned_object_or_404, owned_queryset
from apps.ledger.balances import financial_account_balances

from .models import FinancialAccount


def _data_key(request: HttpRequest) -> bytes:
    return request_data_key(request)


def _readable(account: FinancialAccount, data_key: bytes) -> dict[str, Any]:
    """One account with its encrypted fields opened for the person who owns it."""

    return {
        "account": account,
        "name": account.read_field("name_encrypted", key=data_key),
        "institution": account.read_field("institution_encrypted", key=data_key),
    }


class FinancialAccountListView(LoginRequiredMixin, View):
    """Every account this user has, with what the ledger says each one holds."""

    template_name = "financial_accounts/account_list.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        data_key = _data_key(request)
        balances = financial_account_balances(request.user, data_key=data_key)
        rows = []
        for account in owned_queryset(FinancialAccount, request.user):
            row = _readable(account, data_key)
            row["balances"] = balances.get(account.pk, {})
            rows.append(row)
        return render(request, self.template_name, {"rows": rows})


class FinancialAccountDetailView(LoginRequiredMixin, View):
    template_name = "financial_accounts/account_detail.html"

    def get(self, request: HttpRequest, pk: Any) -> HttpResponse:
        account = get_owned_object_or_404(FinancialAccount, request.user, pk=pk)
        data_key = _data_key(request)
        context = _readable(account, data_key)
        context["balances"] = financial_account_balances(request.user, data_key=data_key).get(
            account.pk, {}
        )
        context["instruments"] = account.payment_instruments.all()
        return render(request, self.template_name, context)
