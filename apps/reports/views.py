"""Report pages.

Nothing here is cached. Every figure on these pages is derived from amounts
encrypted per user, and a cached total is a plaintext copy of somebody's
finances living outside the encrypted store. The pages are cheap to rebuild and
expensive to leak, which settles the trade (specification 22.5).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import View

from apps.categorization.models import Category
from apps.core.errors import ApplicationError
from apps.core.key_scope import request_data_key
from apps.core.ownership import owned_queryset
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument
from apps.reconciliation.services import queue_match_ids

from .activity import account_activity, instrument_activity
from .breakdown import category_breakdown, merchant_breakdown, reconciles
from .forms import ExportRequestForm
from .models import TransactionExport
from .overview import period_overview
from .services import available_exports, create_export, delete_export, read_export
from .spending import month_bounds, monthly_spending
from .workload import outstanding_work

DEFAULT_CURRENCY = "KRW"


def _month(request: HttpRequest) -> tuple[int, int]:
    """The month being reported, defaulting to the current one."""

    today = timezone.localdate()
    try:
        year = int(request.GET.get("year") or today.year)
        month = int(request.GET.get("month") or today.month)
        month_bounds(year, month)
    except (TypeError, ValueError):
        return today.year, today.month
    return year, month


def _range(request: HttpRequest, year: int, month: int) -> tuple[date, date]:
    """An explicit date range if one was given, otherwise the whole month."""

    start, end = month_bounds(year, month)
    try:
        if request.GET.get("start"):
            start = date.fromisoformat(request.GET["start"])
        if request.GET.get("end"):
            end = date.fromisoformat(request.GET["end"])
    except ValueError:
        return month_bounds(year, month)
    if end < start:
        return month_bounds(year, month)
    return start, end


def _owned_id(request: HttpRequest, model: Any, parameter: str) -> Any:
    """An identifier from the query string, only ever one the user owns.

    An identifier the user does not own is dropped rather than applied. Applying
    it would render an empty page implying they had spent nothing.
    """

    requested = request.GET.get(parameter)
    if not requested:
        return None
    return (
        owned_queryset(model, request.user)
        .filter(pk=requested)
        .values_list("pk", flat=True)
        .first()
    )


@method_decorator(never_cache, name="dispatch")
class SpendingReportView(LoginRequiredMixin, View):
    """Spending by category and by merchant, for one period."""

    grouping = "category"
    template_name = "reports/spending_report.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        year, month = _month(request)
        start, end = _range(request, year, month)
        currency = (request.GET.get("currency") or DEFAULT_CURRENCY).upper()
        data_key = request_data_key(request)
        category_id = _owned_id(request, Category, "category")
        build = category_breakdown if self.grouping == "category" else merchant_breakdown
        breakdown = build(
            request.user,
            start=start,
            end=end,
            currency=currency,
            data_key=data_key,
            category_id=category_id,
        )
        month_totals = monthly_spending(
            request.user, year=year, month=month, data_key=data_key
        ).totals(currency)
        whole_month = (start, end) == month_bounds(year, month) and category_id is None
        return render(
            request,
            self.template_name,
            {
                "grouping": self.grouping,
                "breakdown": breakdown,
                "month_totals": month_totals,
                # Only meaningful when the page shows the whole month unfiltered;
                # a narrowed range is *expected* to differ from the month.
                "reconciles": reconciles(breakdown, month_totals) if whole_month else None,
                "year": year,
                "month": month,
                "start": start,
                "end": end,
                "currency": currency,
                "selected_category": category_id,
                "categories": owned_queryset(Category, request.user),
            },
        )


class MerchantReportView(SpendingReportView):
    """The same period, grouped by merchant instead."""

    grouping = "merchant"


@method_decorator(never_cache, name="dispatch")
class AccountReportView(LoginRequiredMixin, View):
    """Activity by account, with card payments shown apart from spending."""

    grouping = "account"
    template_name = "reports/activity_report.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        year, month = _month(request)
        start, end = _range(request, year, month)
        currency = (request.GET.get("currency") or DEFAULT_CURRENCY).upper()
        data_key = request_data_key(request)
        account_id = _owned_id(request, FinancialAccount, "account")
        instrument_id = _owned_id(request, PaymentInstrument, "instrument")
        build = account_activity if self.grouping == "account" else instrument_activity
        report = build(
            request.user,
            start=start,
            end=end,
            currency=currency,
            data_key=data_key,
            account_id=account_id,
            instrument_id=instrument_id,
        )
        return render(
            request,
            self.template_name,
            {
                "report": report,
                "grouping": self.grouping,
                "year": year,
                "month": month,
                "start": start,
                "end": end,
                "currency": currency,
                "selected_account": account_id,
                "selected_instrument": instrument_id,
                "accounts": owned_queryset(FinancialAccount, request.user),
                "instruments": owned_queryset(PaymentInstrument, request.user),
            },
        )


class CardReportView(AccountReportView):
    """The same period, grouped by card instead."""

    grouping = "instrument"


@method_decorator(never_cache, name="dispatch")
class OverviewReportView(LoginRequiredMixin, View):
    """Income against spending, with every exclusion named."""

    template_name = "reports/overview_report.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        year, month = _month(request)
        start, end = _range(request, year, month)
        currency = (request.GET.get("currency") or DEFAULT_CURRENCY).upper()
        data_key = request_data_key(request)
        overview = period_overview(
            request.user, start=start, end=end, currency=currency, data_key=data_key
        )
        return render(
            request,
            self.template_name,
            {
                "overview": overview,
                "figures": overview.figures(),
                "year": year,
                "month": month,
                "start": start,
                "end": end,
                "currency": currency,
            },
        )


@method_decorator(never_cache, name="dispatch")
class WorkloadReportView(LoginRequiredMixin, View):
    """What is still waiting for a decision, and where to make it."""

    template_name = "reports/workload_report.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        # Reconciliation candidates reach the queue counts as identifiers, which
        # is how the observations app stays unaware of this one.
        workload = outstanding_work(request.user, match_ids=queue_match_ids(request.user))
        return render(request, self.template_name, {"workload": workload})


@method_decorator(never_cache, name="dispatch")
class ExportView(LoginRequiredMixin, View):
    """Generate an export, and list the ones still downloadable."""

    template_name = "reports/exports.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, self.template_name, self._context(request, ExportRequestForm()))

    def post(self, request: HttpRequest) -> HttpResponse:
        form = ExportRequestForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, self._context(request, form), status=400)
        try:
            record = create_export(
                user=request.user,
                export_format=form.cleaned_data["export_format"],
                start=form.cleaned_data.get("start"),
                end=form.cleaned_data.get("end"),
                data_key=request_data_key(request),
                passphrase=form.cleaned_data.get("passphrase") or "",
            )
        except ApplicationError as error:
            messages.error(request, error.public_message)
            return render(request, self.template_name, self._context(request, form), status=400)
        messages.success(
            request,
            f"{record.row_count} transaction(s) exported. The file is deleted "
            f"automatically at {record.expires_at:%H:%M}.",
        )
        return redirect("report-exports")

    def _context(self, request: HttpRequest, form: Any) -> dict[str, Any]:
        return {"form": form, "exports": available_exports(request.user)}


@method_decorator(never_cache, name="dispatch")
class ExportDownloadView(LoginRequiredMixin, View):
    """Stream one export to its owner."""

    def get(self, request: HttpRequest, pk: Any) -> HttpResponse:
        try:
            record, payload = read_export(pk, user=request.user)
        except ApplicationError as error:
            messages.error(request, error.public_message)
            return redirect("report-exports")
        content_type = {
            TransactionExport.Format.CSV: "text/csv",
            TransactionExport.Format.JSON: "application/json",
            TransactionExport.Format.ENCRYPTED: "application/octet-stream",
        }[TransactionExport.Format(record.export_format)]
        response = HttpResponse(payload, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{record.filename}"'
        # Never cached anywhere: this body is the user's financial history in
        # readable form.
        response["Cache-Control"] = "private, no-store"
        return response


@method_decorator(never_cache, name="dispatch")
class ExportDeleteView(LoginRequiredMixin, View):
    """Delete one export's file now rather than waiting for its expiry."""

    def post(self, request: HttpRequest, pk: Any) -> HttpResponse:
        try:
            delete_export(pk, user=request.user)
            messages.success(request, "The export file was deleted.")
        except ApplicationError as error:
            messages.error(request, error.public_message)
        return redirect("report-exports")
