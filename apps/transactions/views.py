from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import FormView, View

from apps.core.errors import ApplicationError
from apps.core.key_scope import request_data_key, request_search_key
from apps.core.ownership import get_owned_object_or_404, owned_queryset

from .deletion import delete_transaction
from .forms import ManualTransactionForm
from .models import CanonicalTransaction
from .money import read_money


def _data_key(request: HttpRequest) -> bytes:
    """The requesting user's own key. Manual entry is encrypted like everything else."""

    return request_data_key(request)


class ManualTransactionCreateView(LoginRequiredMixin, FormView):  # type: ignore[type-arg]
    template_name = "transactions/transaction_form.html"
    form_class = ManualTransactionForm
    success_url = reverse_lazy("transaction-list")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form: ManualTransactionForm) -> HttpResponse:
        form.save(
            data_key=_data_key(self.request),
            blind_index_key=request_search_key(self.request),
        )
        messages.success(self.request, "Transaction saved for review.")
        return super().form_valid(form)


class ManualTransactionUpdateView(LoginRequiredMixin, FormView):  # type: ignore[type-arg]
    template_name = "transactions/transaction_form.html"
    form_class = ManualTransactionForm
    success_url = reverse_lazy("transaction-list")
    instance: CanonicalTransaction | None = None

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        # The authentication check has to come first. ``LoginRequiredMixin`` does
        # it inside its own ``dispatch``, which this override precedes in the
        # MRO — so looking the row up here would run a database query for an
        # anonymous request and fail on the assertion rather than redirecting.
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.instance = get_owned_object_or_404(CanonicalTransaction, request.user, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)  # type: ignore[return-value]

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        assert self.instance is not None
        kwargs.update(user=self.request.user, instance=self.instance)
        return kwargs

    def form_valid(self, form: ManualTransactionForm) -> HttpResponse:
        form.save(
            data_key=_data_key(self.request),
            blind_index_key=request_search_key(self.request),
        )
        messages.success(self.request, "Transaction updated.")
        return super().form_valid(form)


class TransactionListView(LoginRequiredMixin, FormView):  # type: ignore[type-arg]
    template_name = "transactions/transaction_list.html"
    form_class = ManualTransactionForm

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        assert request.user.pk is not None
        return render(
            request,
            self.template_name,
            {"transactions": owned_queryset(CanonicalTransaction, request.user)},
        )


class TransactionDeleteView(LoginRequiredMixin, View):
    """Withdraw a transaction, behind a page that says what that means.

    The confirmation is a separate GET rather than a browser dialog because the
    consequences are not obvious from the button: the ledger is reversed, the
    observations that fed the transaction go back into the review queue, and
    none of it is undone by clicking again. A person is owed the chance to read
    that before it happens.
    """

    def get(self, request: HttpRequest, pk: Any) -> HttpResponse:
        transaction = get_owned_object_or_404(CanonicalTransaction, request.user, pk=pk)
        return render(
            request,
            "transactions/transaction_confirm_delete.html",
            {
                "transaction": transaction,
                "posted_entry_count": transaction.ledger_entries.count(),
                "linked_observation_count": transaction.observations.count(),
            },
        )

    def post(self, request: HttpRequest, pk: Any) -> HttpResponse:
        transaction = get_owned_object_or_404(CanonicalTransaction, request.user, pk=pk)
        try:
            result = delete_transaction(
                transaction.pk,
                user=request.user,
                reason=request.POST.get("reason", ""),
                confirmed=request.POST.get("confirm") == "yes",
                data_key=_data_key(request),
            )
        except ApplicationError as error:
            messages.error(request, error.public_message)
            return redirect("transaction-delete", pk=transaction.pk)
        released = result.released_observation_count
        messages.success(
            request,
            "Transaction deleted and its ledger entries reversed."
            + (f" {released} observation(s) returned to review." if released else ""),
        )
        return redirect("transaction-list")


class TransactionDetailView(LoginRequiredMixin, View):
    """One transaction, decrypted for its owner.

    Specification section 24 gives a transaction its own address, separate from
    the edit form. A link to a transaction should show it, not open it for
    editing — and a confirmed transaction has no edit form to open.
    """

    template_name = "transactions/transaction_detail.html"

    def get(self, request: HttpRequest, pk: Any) -> HttpResponse:
        transaction = get_owned_object_or_404(CanonicalTransaction, request.user, pk=pk)
        data_key = _data_key(request)
        return render(
            request,
            self.template_name,
            {
                "transaction": transaction,
                "merchant": transaction.read_field("merchant_encrypted", key=data_key),
                "counterparty": transaction.read_field("counterparty_encrypted", key=data_key),
                "notes": transaction.read_field("notes_encrypted", key=data_key),
                "amount": read_money(transaction, "amount_encrypted", data_key=data_key),
                "entry_count": transaction.ledger_entries.count(),
            },
        )
