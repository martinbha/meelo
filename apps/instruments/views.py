"""The cards and other payment instruments, at the paths section 24 names.

Read-only for now. Creating instruments and mapping them to accounts is #185.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.generic import View

from apps.core.key_scope import request_data_key
from apps.core.ownership import get_owned_object_or_404, owned_queryset

from .models import PaymentInstrument


def _data_key(request: HttpRequest) -> bytes:
    return request_data_key(request)


def _readable(instrument: PaymentInstrument, data_key: bytes) -> dict[str, Any]:
    return {
        "instrument": instrument,
        "name": instrument.read_field("name_encrypted", key=data_key),
        "issuer": instrument.read_field("issuer_encrypted", key=data_key),
    }


class PaymentInstrumentListView(LoginRequiredMixin, View):
    template_name = "instruments/instrument_list.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        data_key = _data_key(request)
        rows = [
            _readable(instrument, data_key)
            for instrument in owned_queryset(PaymentInstrument, request.user).select_related(
                "financial_account", "settlement_account"
            )
        ]
        return render(request, self.template_name, {"rows": rows})


class PaymentInstrumentDetailView(LoginRequiredMixin, View):
    template_name = "instruments/instrument_detail.html"

    def get(self, request: HttpRequest, pk: Any) -> HttpResponse:
        instrument = get_owned_object_or_404(PaymentInstrument, request.user, pk=pk)
        return render(request, self.template_name, _readable(instrument, _data_key(request)))
