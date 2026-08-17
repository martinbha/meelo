"""Reviewing proposed relationships between observations.

Duplicates are compared side by side and merged only when a person picks the
winning row. Other relationship kinds — settlements, transfers, refunds — are
confirmed or dismissed without touching either observation.

Every proposal arrives with its reasons attached. A score alone would leave a
reviewer nothing to check but the number, and deferring to the number is the
one thing this queue exists to prevent.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.generic import View

from apps.core.errors import ApplicationError
from apps.core.key_management import get_user_data_key, load_master_key
from apps.core.ownership import get_owned_object_or_404
from apps.observations.review import decrypt_observation

from .explanations import describe_features, proposed_transaction_type_label
from .forms import ManualLinkForm
from .models import ReconciliationMatch
from .refunds import confirm_refund_match
from .services import (
    confirm_duplicate_match,
    confirm_match,
    decrypt_match_features,
    link_observations,
    open_matches,
    reject_match,
)
from .transfers import confirm_internal_transfer


def _data_key(request: HttpRequest) -> bytes:
    return get_user_data_key(user=request.user, actor=request.user, master_key=load_master_key())


class MatchQueueView(LoginRequiredMixin, View):
    """Everything reconciliation has proposed and nobody has decided yet."""

    def get(self, request: HttpRequest) -> HttpResponse:
        requested = request.GET.get("type") or None
        if requested is not None and requested not in ReconciliationMatch.MatchType.values:
            requested = None
        data_key = _data_key(request)
        matches = open_matches(request.user, match_type=requested).select_related(
            "left_observation", "right_observation"
        )
        rows = [
            {
                "match": match,
                "reasons": describe_features(decrypt_match_features(match, data_key=data_key)),
                "proposed_type": proposed_transaction_type_label(match.match_type),
            }
            for match in matches
        ]
        return render(
            request,
            "reconciliation/match_queue.html",
            {
                "rows": rows,
                "selected_type": requested,
                "match_types": list(ReconciliationMatch.MatchType),
            },
        )


class MatchDetailView(LoginRequiredMixin, View):
    """One candidate, with both rows shown side by side for comparison."""

    def get(self, request: HttpRequest, pk: Any) -> HttpResponse:
        match = get_owned_object_or_404(ReconciliationMatch, request.user, pk=pk)
        data_key = _data_key(request)
        candidates = [
            {
                "label": label,
                "observation": observation,
                "values": decrypt_observation(observation, user=request.user, data_key=data_key),
            }
            for label, observation in (
                ("Row A", match.left_observation),
                ("Row B", match.right_observation),
            )
        ]
        return render(
            request,
            "reconciliation/match_detail.html",
            {
                "match": match,
                "candidates": candidates,
                "reasons": describe_features(decrypt_match_features(match, data_key=data_key)),
                "proposed_type": proposed_transaction_type_label(match.match_type),
                "is_duplicate": (
                    match.match_type == ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION
                ),
            },
        )


class MatchLinkView(LoginRequiredMixin, View):
    """Record a relationship the matcher missed but the user can see."""

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(
            request,
            "reconciliation/match_link.html",
            {"form": ManualLinkForm(user=request.user)},
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        form = ManualLinkForm(request.POST, user=request.user)
        if not form.is_valid():
            return render(request, "reconciliation/match_link.html", {"form": form}, status=400)
        try:
            match = link_observations(
                user=request.user,
                left_observation_id=form.cleaned_data["left_observation"].pk,
                right_observation_id=form.cleaned_data["right_observation"].pk,
                match_type=form.cleaned_data["match_type"],
                data_key=_data_key(request),
            )
        except ApplicationError as error:
            messages.error(request, error.message)
            return render(request, "reconciliation/match_link.html", {"form": form}, status=400)
        messages.success(request, "The rows were linked. Confirm to apply the relationship.")
        return redirect("match-detail", pk=match.pk)


class MatchActionView(LoginRequiredMixin, View):
    """Confirm or dismiss one candidate."""

    def post(self, request: HttpRequest, pk: Any, action: str) -> HttpResponse:
        match = get_owned_object_or_404(ReconciliationMatch, request.user, pk=pk)
        if action not in {"confirm", "reject"}:
            raise Http404
        try:
            if action == "reject":
                reject_match(match.pk, user=request.user)
                messages.success(request, "The candidate was dismissed.")
            elif match.match_type == ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION:
                winner_id = request.POST.get("winner")
                if not winner_id:
                    messages.error(request, "Choose which row to keep before merging.")
                    return redirect("match-detail", pk=match.pk)
                confirm_duplicate_match(match.pk, user=request.user, winner_id=winner_id)
                messages.success(request, "The duplicate was merged.")
            elif match.match_type == ReconciliationMatch.MatchType.INTERNAL_TRANSFER:
                confirm_internal_transfer(match.pk, user=request.user, data_key=_data_key(request))
                messages.success(request, "The transfer was recorded as one event.")
            elif match.match_type == ReconciliationMatch.MatchType.REFUND_MATCH:
                confirm_refund_match(match.pk, user=request.user, data_key=_data_key(request))
                messages.success(request, "The refund was applied to its purchase category.")
            else:
                confirm_match(match.pk, user=request.user)
                messages.success(request, "The match was confirmed.")
        except ApplicationError as error:
            messages.error(request, error.message)
            return redirect("match-detail", pk=match.pk)
        return redirect("match-queue")
