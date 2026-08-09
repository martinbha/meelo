from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.generic import FormView

from .forms import ManualTransactionForm
from .models import CanonicalTransaction


class ManualTransactionCreateView(LoginRequiredMixin, FormView):  # type: ignore[type-arg]
    template_name = "transactions/transaction_form.html"
    form_class = ManualTransactionForm
    success_url = reverse_lazy("transaction-list")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form: ManualTransactionForm) -> HttpResponse:
        form.save()
        messages.success(self.request, "Transaction saved for review.")
        return super().form_valid(form)


class ManualTransactionUpdateView(LoginRequiredMixin, FormView):  # type: ignore[type-arg]
    template_name = "transactions/transaction_form.html"
    form_class = ManualTransactionForm
    success_url = reverse_lazy("transaction-list")
    instance: CanonicalTransaction | None = None

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        assert request.user.pk is not None
        candidate = CanonicalTransaction.objects.filter(
            pk=kwargs["pk"], user_id=request.user.pk
        ).first()
        if candidate is None:
            raise Http404
        self.instance = candidate
        return super().dispatch(request, *args, **kwargs)  # type: ignore[return-value]

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        assert self.instance is not None
        kwargs.update(user=self.request.user, instance=self.instance)
        return kwargs

    def form_valid(self, form: ManualTransactionForm) -> HttpResponse:
        form.save()
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
            {"transactions": CanonicalTransaction.objects.filter(user_id=request.user.pk)},
        )
