from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.generic import FormView

from apps.core.key_management import get_user_data_key, load_master_key
from apps.core.ownership import get_owned_object_or_404, owned_queryset

from .forms import ManualTransactionForm
from .models import CanonicalTransaction


def _data_key(request: HttpRequest) -> bytes:
    """The requesting user's own key. Manual entry is encrypted like everything else."""

    return get_user_data_key(user=request.user, actor=request.user, master_key=load_master_key())


class ManualTransactionCreateView(LoginRequiredMixin, FormView):  # type: ignore[type-arg]
    template_name = "transactions/transaction_form.html"
    form_class = ManualTransactionForm
    success_url = reverse_lazy("transaction-list")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form: ManualTransactionForm) -> HttpResponse:
        form.save(data_key=_data_key(self.request))
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
        form.save(data_key=_data_key(self.request))
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
