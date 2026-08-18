"""The review interface: the screenshot on one side, the parsed rows on the other.

Every view here is owner-scoped. The screenshot itself is served through
:class:`DocumentImageView` rather than from a public media directory, because a
bank statement image must never be reachable by anyone who guesses a URL.
"""

from __future__ import annotations

import json
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.generic import View

from apps.core.errors import ApplicationError
from apps.core.key_scope import request_data_key
from apps.core.ownership import get_owned_object_or_404, owned_queryset
from apps.processing.forms import DocumentOverrideForm
from apps.processing.models import SourceDocument
from apps.processing.overrides import set_document_overrides
from apps.processing.storage import document_directory

# The view layer is the only place the two apps meet. Reconciliation
# depends on observations' services, never on this module, so importing it
# here composes the two without creating a cycle.
from apps.reconciliation.services import queue_match_ids

from .forms import MergeForm, ObservationCorrectionForm, ReviewActionForm
from .models import ImportedObservation
from .queue import QueueFilter, review_queue
from .reprocessing import latest_run, request_reprocess
from .review import (
    accept_observation,
    correct_observation,
    decrypt_observation,
    merge_observations,
    reject_observation,
)

#: Image types a stored original may have, mapped to their content type.
IMAGE_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _data_key(request: HttpRequest) -> bytes:
    return request_data_key(request)


def _positive_int(value: str | None, *, default: int) -> int:
    """Read a query-string integer, falling back rather than erroring."""

    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _key_version(request: HttpRequest) -> int:
    return int(getattr(request.user, "encryption_key_version", 1))


class ReviewQueueView(LoginRequiredMixin, View):
    """The prioritized list of everything still awaiting a decision."""

    def get(self, request: HttpRequest) -> HttpResponse:
        raw_filters = request.GET.getlist("filter")
        known = {name.value for name in QueueFilter}
        selected = [name for name in raw_filters if name in known]
        page = review_queue(
            request.user,
            filters=selected,
            # Reconciliation candidates are injected here rather than queried
            # inside the queue, so the observations app never has to import the
            # reconciliation app that already depends on it.
            match_ids=queue_match_ids(request.user),
            page_number=_positive_int(request.GET.get("page"), default=1),
            page_size=_positive_int(request.GET.get("page_size"), default=25),
        )
        # Templates cannot index a dict by a variable key, so the per-filter
        # counts are resolved here rather than in the page.
        options = [
            {
                "value": name.value,
                "label": name.value.replace("_", " "),
                "count": page.counts.get(name.value, 0),
                "selected": name.value in selected,
            }
            for name in QueueFilter
        ]
        return render(
            request,
            "observations/review_queue.html",
            {"page": page, "selected_filters": selected, "filter_options": options},
        )


class ObservationReviewView(LoginRequiredMixin, View):
    """One document side by side with the rows parsed from it."""

    def get(self, request: HttpRequest, pk: Any) -> HttpResponse:
        document = get_owned_object_or_404(SourceDocument, request.user, pk=pk)
        data_key = _data_key(request)
        rows = []
        for observation in (
            owned_queryset(ImportedObservation, request.user)
            .filter(source_document=document)
            .order_by("row_index", "created_at")
        ):
            decrypted = decrypt_observation(observation, user=request.user, data_key=data_key)
            rows.append(
                {
                    "observation": observation,
                    "values": decrypted,
                    "region": _region(decrypted.source_region),
                    "form": ObservationCorrectionForm(
                        user=request.user,
                        initial={
                            "occurred_at": observation.occurred_at,
                            "posted_at": observation.posted_at,
                            "merchant": decrypted.merchant,
                            "amount_minor": decrypted.amount_minor,
                            "currency": observation.currency,
                            "direction": observation.direction,
                            "transaction_type_guess": observation.transaction_type_guess,
                            "installment_months": observation.installment_months,
                            "financial_account_guess": observation.financial_account_guess_id,
                            "payment_instrument_guess": observation.payment_instrument_guess_id,
                            "category_guess": observation.category_guess_id,
                        },
                    ),
                }
            )
        return render(
            request,
            "observations/review_detail.html",
            {
                "document": document,
                "rows": rows,
                "latest_run": latest_run(document),
                "action_form": ReviewActionForm(user=request.user),
                "override_form": DocumentOverrideForm(
                    initial={
                        "source_type": document.source_type_override,
                        "institution": document.institution_override,
                    }
                ),
                "has_image": _image_path(document) is not None,
            },
        )


def _region(payload: str) -> dict[str, int] | None:
    """Decode a stored source region into percentages the template can use."""

    if not payload:
        return None
    try:
        region = json.loads(payload)
        return {key: int(region[key]) for key in ("left", "top", "right", "bottom")}
    except (ValueError, KeyError, TypeError):
        return None


def _image_path(document: SourceDocument) -> Any:
    """Locate the stored original, if retention has not removed it yet."""

    if document.original_deleted_at is not None:
        return None
    try:
        directory = document_directory(document.pk)
    except ValueError:
        return None
    for suffix in IMAGE_CONTENT_TYPES:
        candidate = directory / f"original{suffix}"
        if candidate.is_file():
            return candidate
    return None


class DocumentImageView(LoginRequiredMixin, View):
    """Serve a stored screenshot to its owner and to nobody else."""

    def get(self, request: HttpRequest, pk: Any) -> FileResponse:
        document = get_owned_object_or_404(SourceDocument, request.user, pk=pk)
        path = _image_path(document)
        if path is None:
            raise Http404
        response = FileResponse(
            path.open("rb"), content_type=IMAGE_CONTENT_TYPES[path.suffix.lower()]
        )
        # The original is private user data: never cached by a shared proxy.
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class ObservationActionView(LoginRequiredMixin, View):
    """Apply one reviewer decision to one observation."""

    def post(self, request: HttpRequest, pk: Any, action: str) -> HttpResponse:
        observation = get_owned_object_or_404(ImportedObservation, request.user, pk=pk)
        handler = {
            "correct": self._correct,
            "accept": self._accept,
            "reject": self._reject,
            "merge": self._merge,
        }.get(action)
        if handler is None:
            raise Http404
        try:
            handler(request, observation)
        except ApplicationError as error:
            messages.error(request, error.message)
        return redirect("observation-review", pk=observation.source_document_id)

    def _correct(self, request: HttpRequest, observation: ImportedObservation) -> None:
        form = ObservationCorrectionForm(request.POST, user=request.user)
        if not form.is_valid():
            messages.error(request, "The correction could not be saved; check the fields.")
            return
        corrections = form.corrections()
        if not corrections:
            messages.info(request, "Nothing was changed.")
            return
        correct_observation(
            observation.pk,
            user=request.user,
            data_key=_data_key(request),
            key_version=_key_version(request),
            corrections=corrections,
        )
        messages.success(request, "The correction was saved.")

    def _accept(self, request: HttpRequest, observation: ImportedObservation) -> None:
        form = ReviewActionForm(request.POST, user=request.user)
        if not form.is_valid():
            messages.error(request, "The acceptance could not be processed.")
            return
        accept_observation(
            observation.pk,
            user=request.user,
            data_key=_data_key(request),
            financial_account=form.cleaned_data.get("financial_account"),
            transaction_type=form.cleaned_data.get("transaction_type") or None,
            confirmed=bool(form.cleaned_data.get("confirmed")),
        )
        messages.success(request, "The transaction was recorded.")

    def _reject(self, request: HttpRequest, observation: ImportedObservation) -> None:
        reject_observation(observation.pk, user=request.user, reason=request.POST.get("reason", ""))
        messages.success(request, "The row was rejected and will not be reported.")

    def _merge(self, request: HttpRequest, observation: ImportedObservation) -> None:
        form = MergeForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Select at least one duplicate to merge.")
            return
        merge_observations(
            user=request.user,
            winner_id=observation.pk,
            duplicate_ids=form.cleaned_data["duplicate_ids"],
        )
        messages.success(request, "The duplicates were merged.")


class DocumentOverrideView(LoginRequiredMixin, View):
    """Record what a reviewer says this screenshot is.

    Saving an override does not itself re-run anything. The reviewer is told to
    ask for another pass, because reprocessing costs a minute of OCR and a
    reviewer correcting the type and the institution in turn should not pay for
    it twice.
    """

    def post(self, request: HttpRequest, pk: Any) -> HttpResponse:
        document = get_owned_object_or_404(SourceDocument, request.user, pk=pk)
        form = DocumentOverrideForm(request.POST)
        if not form.is_valid():
            messages.error(request, "That is not a screenshot type this system recognises.")
            return redirect("observation-review", pk=document.pk)
        try:
            change = set_document_overrides(
                document.pk,
                user=request.user,
                source_type=form.cleaned_data["source_type"],
                institution=form.cleaned_data["institution"],
            )
        except ApplicationError as error:
            messages.error(request, error.message)
            return redirect("observation-review", pk=document.pk)
        if not change.changed:
            messages.info(request, "That is already how this screenshot is being read.")
        elif change.cleared:
            messages.success(
                request, "Detection restored. Re-run OCR to read the screenshot again."
            )
        else:
            messages.success(request, "Saved. Re-run OCR to read the screenshot again.")
        return redirect("observation-review", pk=document.pk)


class DocumentReprocessView(LoginRequiredMixin, View):
    """Ask for another OCR pass over a badly read screenshot."""

    def post(self, request: HttpRequest, pk: Any) -> HttpResponse:
        document = get_owned_object_or_404(SourceDocument, request.user, pk=pk)
        try:
            request_reprocess(document.pk, user=request.user)
        except ApplicationError as error:
            messages.error(request, error.message)
        else:
            messages.success(request, "The screenshot was queued for another pass.")
        return redirect("observation-review", pk=document.pk)
