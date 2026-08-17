"""Report pages.

Nothing here is cached. Every figure on these pages is derived from amounts
encrypted per user, and a cached total is a plaintext copy of somebody's
finances living outside the encrypted store. The pages are cheap to rebuild and
expensive to leak, which settles the trade (specification 22.5).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import View

from apps.categorization.models import Category
from apps.core.key_management import get_user_data_key, load_master_key
from apps.core.ownership import owned_queryset

from .breakdown import category_breakdown, merchant_breakdown, reconciles
from .spending import month_bounds, monthly_spending

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


def _category_filter(request: HttpRequest) -> Any:
    """A category to narrow to, only ever one the requesting user owns."""

    requested = request.GET.get("category")
    if not requested:
        return None
    return (
        owned_queryset(Category, request.user)
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
        data_key = get_user_data_key(
            user=request.user, actor=request.user, master_key=load_master_key()
        )
        category_id = _category_filter(request)
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
