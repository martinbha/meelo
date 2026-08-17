"""Correcting a category from review, and choosing how far the correction goes.

The page shows what each scope would reach *before* anything is written, because
the difference between "this coffee" and "every coffee I have ever bought" is not
something a person should discover afterwards.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.generic import View

from apps.core.crypto import read_model_field
from apps.core.errors import ApplicationError
from apps.core.key_management import derive_blind_index_key, get_user_data_key, load_master_key
from apps.core.ownership import get_owned_object_or_404
from apps.transactions.models import CanonicalTransaction

from .forms import CategoryCorrectionForm
from .rule_creation import SCOPE_LABELS, RuleScope, create_rule_from_correction, preview_rule


def _keys(request: HttpRequest) -> bytes:
    return get_user_data_key(user=request.user, actor=request.user, master_key=load_master_key())


def _merchant(transaction: CanonicalTransaction, *, data_key: bytes) -> str:
    """The merchant this rule will be keyed on.

    Rows written before field encryption reached this model hold the name in
    clear. Those are recognised by the absence of an envelope prefix rather than
    by catching a decryption failure: a real failure has to stay loud, because
    indexing a ciphertext would produce a rule that can never fire and nothing
    would say so (#163).
    """

    return read_model_field(transaction, "merchant_encrypted", key=data_key)


class CategoryCorrectionView(LoginRequiredMixin, View):
    """Re-file one transaction, optionally writing a rule for the next one."""

    def get(self, request: HttpRequest, pk: Any) -> HttpResponse:
        transaction = get_owned_object_or_404(CanonicalTransaction, request.user, pk=pk)
        return render(
            request,
            "categorization/category_correction.html",
            self._context(request, transaction, CategoryCorrectionForm(user=request.user)),
        )

    def post(self, request: HttpRequest, pk: Any) -> HttpResponse:
        transaction = get_owned_object_or_404(CanonicalTransaction, request.user, pk=pk)
        form = CategoryCorrectionForm(request.POST, user=request.user)
        if not form.is_valid():
            return render(
                request,
                "categorization/category_correction.html",
                self._context(request, transaction, form),
                status=400,
            )
        data_key = _keys(request)
        try:
            result = create_rule_from_correction(
                user=request.user,
                transaction=transaction,
                category=form.cleaned_data["category"],
                scope=form.cleaned_data["scope"],
                merchant=_merchant(transaction, data_key=data_key),
                encryption_key=data_key,
                # The same derivation import used, so the rule's index
                # matches the one on the transaction it was written from.
                blind_index_key=derive_blind_index_key(data_key),
                apply_to_existing=form.cleaned_data["apply_to_existing"],
            )
        except ApplicationError as error:
            messages.error(request, error.message)
            return render(
                request,
                "categorization/category_correction.html",
                self._context(request, transaction, form),
                status=400,
            )
        if result.rule is None:
            messages.success(request, "The category was changed for this transaction.")
        else:
            messages.success(
                request,
                f"The category was changed and a rule was saved. "
                f"{result.reclassified} unconfirmed transactions were updated.",
            )
        return redirect("transaction-list")

    def _context(
        self, request: HttpRequest, transaction: CanonicalTransaction, form: Any
    ) -> dict[str, Any]:
        previews = []
        for scope in RuleScope:
            try:
                previews.append(
                    (
                        SCOPE_LABELS[scope],
                        preview_rule(user=request.user, transaction=transaction, scope=scope),
                    )
                )
            except ApplicationError:
                # A scope this transaction cannot support — a card rule with no
                # card — is simply not offered a preview.
                continue
        return {"transaction": transaction, "form": form, "previews": previews}
