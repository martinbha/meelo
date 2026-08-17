"""Reviewing proposed relationships between observations.

Duplicates are compared side by side and merged only when a person picks the
winning row. Other relationship kinds — settlements, transfers, refunds — are
confirmed or dismissed without touching either observation.
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

from .models import ReconciliationMatch
from .services import confirm_duplicate_match, confirm_match, open_matches, reject_match
from .transfers import confirm_internal_transfer


class MatchQueueView(LoginRequiredMixin, View):
    """Everything reconciliation has proposed and nobody has decided yet."""

    def get(self, request: HttpRequest) -> HttpResponse:
        requested = request.GET.get("type") or None
        if requested is not None and requested not in ReconciliationMatch.MatchType.values:
            requested = None
        matches = open_matches(request.user, match_type=requested).select_related(
            "left_observation", "right_observation"
        )
        return render(
            request,
            "reconciliation/match_queue.html",
            {
                "matches": matches,
                "selected_type": requested,
                "match_types": list(ReconciliationMatch.MatchType),
            },
        )


class MatchDetailView(LoginRequiredMixin, View):
    """One candidate, with both rows shown side by side for comparison."""

    def get(self, request: HttpRequest, pk: Any) -> HttpResponse:
        match = get_owned_object_or_404(ReconciliationMatch, request.user, pk=pk)
        data_key = get_user_data_key(
            user=request.user, actor=request.user, master_key=load_master_key()
        )
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
                "is_duplicate": (
                    match.match_type == ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION
                ),
            },
        )


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
                confirm_internal_transfer(
                    match.pk,
                    user=request.user,
                    data_key=get_user_data_key(
                        user=request.user, actor=request.user, master_key=load_master_key()
                    ),
                )
                messages.success(request, "The transfer was recorded as one event.")
            else:
                confirm_match(match.pk, user=request.user)
                messages.success(request, "The match was confirmed.")
        except ApplicationError as error:
            messages.error(request, error.message)
            return redirect("match-detail", pk=match.pk)
        return redirect("match-queue")
