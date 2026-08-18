"""Parser candidates, kept strictly apart from confirmed financial history.

An :class:`ImportedObservation` is what a parser thought it saw on one
screenshot row. It is never financial history: reports and the ledger read
:class:`~apps.transactions.models.CanonicalTransaction`, and an observation
only reaches that stage when a reviewer accepts it.

That separation is what lets a document be reprocessed safely — a new OCR run
creates new observations and leaves every confirmed transaction untouched.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.categorization.models import Category
from apps.core.currencies import is_supported, normalize_code
from apps.core.encrypted_fields import EncryptedFieldsMixin
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument
from apps.ocr.models import OcrRun
from apps.processing.models import SourceDocument
from apps.transactions.models import CanonicalTransaction


class ImportedObservation(EncryptedFieldsMixin, models.Model):
    """One parsed row awaiting review."""

    encrypted_fields = (
        "merchant_raw_encrypted",
        "merchant_normalized_encrypted",
        "counterparty_raw_encrypted",
        "amount_encrypted",
        "balance_after_encrypted",
        "approval_code_encrypted",
        "source_region_json_encrypted",
    )

    class ReviewStatus(models.TextChoices):
        UNREVIEWED = "unreviewed", "Unreviewed"
        ACCEPTED = "accepted", "Accepted"
        CORRECTED = "corrected", "Corrected"
        REJECTED = "rejected", "Rejected"
        MERGED = "merged", "Merged"

    #: Statuses that have left the queue and must not feed reports.
    RESOLVED_STATUSES = frozenset({ReviewStatus.REJECTED, ReviewStatus.MERGED})
    #: Statuses a reviewer has actioned in the affirmative.
    ACCEPTED_STATUSES = frozenset({ReviewStatus.ACCEPTED, ReviewStatus.CORRECTED})

    class Direction(models.TextChoices):
        DEBIT = "debit", "Debit"
        CREDIT = "credit", "Credit"
        UNKNOWN = "unknown", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="imported_observations",
    )
    source_document = models.ForeignKey(
        SourceDocument,
        on_delete=models.CASCADE,
        related_name="imported_observations",
    )
    #: The OCR run this observation was parsed from. Retained so a reprocessed
    #: document can show which run produced which candidate.
    ocr_run = models.ForeignKey(
        OcrRun,
        on_delete=models.SET_NULL,
        related_name="imported_observations",
        blank=True,
        null=True,
    )
    #: Position of the row on the screenshot, so review lists read top to bottom.
    row_index = models.PositiveIntegerField(default=0)

    financial_account_guess = models.ForeignKey(
        FinancialAccount,
        on_delete=models.SET_NULL,
        related_name="observation_guesses",
        blank=True,
        null=True,
    )
    payment_instrument_guess = models.ForeignKey(
        PaymentInstrument,
        on_delete=models.SET_NULL,
        related_name="observation_guesses",
        blank=True,
        null=True,
    )
    category_guess = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        related_name="observation_guesses",
        blank=True,
        null=True,
    )

    occurred_at = models.DateField(blank=True, null=True)
    posted_at = models.DateField(blank=True, null=True)
    merchant_raw_encrypted = models.TextField(blank=True)
    merchant_normalized_encrypted = models.TextField(blank=True)
    merchant_blind_index = models.CharField(max_length=128, blank=True)
    counterparty_raw_encrypted = models.TextField(blank=True)
    amount_encrypted = models.TextField(blank=True)
    currency = models.CharField(max_length=3, blank=True)
    direction = models.CharField(
        max_length=16, choices=Direction.choices, default=Direction.UNKNOWN
    )
    balance_after_encrypted = models.TextField(blank=True)
    approval_code_encrypted = models.TextField(blank=True)
    installment_months = models.PositiveSmallIntegerField(blank=True, null=True)
    transaction_type_guess = models.CharField(
        max_length=32,
        choices=CanonicalTransaction.TransactionType.choices,
        default=CanonicalTransaction.TransactionType.UNKNOWN,
    )

    #: Confidences are stored apart so review can tell a clean parse of a bad
    #: scan from a poor parse of a sharp one.
    ocr_confidence = models.FloatField(default=0.0)
    parser_confidence = models.FloatField(default=0.0)
    overall_confidence = models.FloatField(default=0.0)
    source_region_json_encrypted = models.TextField(blank=True)

    #: Parser provenance, so a fixed parser can be told from a fixed screenshot.
    parser_name = models.CharField(max_length=64, blank=True)
    parser_version = models.CharField(max_length=32, blank=True)
    parser_output_version = models.PositiveSmallIntegerField(default=1)

    #: Review flags the parser raised, e.g. missing fields or a broken balance
    #: chain. Names only — never values. Kept for display and explanation.
    review_flags = models.JSONField(default=list, blank=True)
    requires_review = models.BooleanField(default=True)

    # Queryable projections of ``review_flags``. They exist because the review
    # queue must filter and rank in the database — JSON containment is not
    # portable across the databases this project runs on, and ranking in Python
    # would only order rows within a page rather than across the whole queue.
    amount_uncertain = models.BooleanField(default=False)
    balance_mismatched = models.BooleanField(default=False)
    has_missing_fields = models.BooleanField(default=False)
    is_settlement_candidate = models.BooleanField(default=False)
    #: Worst-problem score used to sort the queue. Zero means nothing is wrong.
    risk_score = models.PositiveSmallIntegerField(default=0)

    review_status = models.CharField(
        max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.UNREVIEWED
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviewed_observations",
        blank=True,
        null=True,
    )
    reviewed_at = models.DateTimeField(blank=True, null=True)
    #: Field names a reviewer corrected, for the audit trail and for measuring
    #: where the parsers are weakest.
    corrected_fields = models.JSONField(default=list, blank=True)
    canonical_transaction = models.ForeignKey(
        CanonicalTransaction,
        on_delete=models.SET_NULL,
        related_name="observations",
        blank=True,
        null=True,
    )
    #: Set when this observation was merged into another one.
    merged_into = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="merged_from",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("source_document_id", "row_index", "created_at")
        constraints = [
            # One parser version may produce a given row of a given OCR run
            # exactly once, which is what makes re-import idempotent.
            models.UniqueConstraint(
                fields=("ocr_run", "parser_name", "parser_version", "row_index"),
                name="observation_unique_per_run_row",
            ),
            models.CheckConstraint(
                condition=models.Q(overall_confidence__gte=0.0, overall_confidence__lte=1.0),
                name="observation_confidence_range",
            ),
            # A row folded into another is not history of its own. Holding both
            # a merge target and a transaction would report the same money
            # twice — once through the surviving row, once through this one.
            models.CheckConstraint(
                condition=models.Q(merged_into__isnull=True)
                | models.Q(canonical_transaction__isnull=True),
                name="observation_merged_rows_have_no_transaction",
            ),
            # And a merge is not something that can happen quietly: the status
            # has to say so, because that is what keeps the row out of reports.
            # Spelled out rather than referenced, because a nested Meta cannot
            # see the enclosing class's own TextChoices.
            models.CheckConstraint(
                condition=models.Q(merged_into__isnull=True) | models.Q(review_status="merged"),
                name="observation_merged_rows_say_so",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "review_status"), name="observation_user_status_idx"),
            models.Index(fields=("user", "occurred_at"), name="observation_user_date_idx"),
            models.Index(fields=("user", "merchant_blind_index"), name="observation_merchant_idx"),
            models.Index(
                fields=("source_document", "row_index"), name="observation_document_row_idx"
            ),
            # The queue's default ordering: open rows, worst problems first.
            models.Index(
                fields=("user", "review_status", "-risk_score"),
                name="observation_queue_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.currency:
            self.currency = normalize_code(self.currency)
            if not is_supported(self.currency):
                errors["currency"] = (
                    f"'{self.currency}' is not a currency this application supports."
                )
        for name, value in (
            ("ocr_confidence", self.ocr_confidence),
            ("parser_confidence", self.parser_confidence),
            ("overall_confidence", self.overall_confidence),
        ):
            if not 0.0 <= value <= 1.0:
                errors[name] = "Confidence must be between zero and one."
        if self.posted_at and self.occurred_at and self.posted_at < self.occurred_at:
            errors["posted_at"] = "Posted date cannot be earlier than the occurred date."

        related: dict[str, tuple[Any, object]] = {
            "source_document": (SourceDocument, self.source_document_id),
            "financial_account_guess": (FinancialAccount, self.financial_account_guess_id),
            "payment_instrument_guess": (PaymentInstrument, self.payment_instrument_guess_id),
            "category_guess": (Category, self.category_guess_id),
            "canonical_transaction": (CanonicalTransaction, self.canonical_transaction_id),
        }
        for field_name, (model, object_id) in related.items():
            if not object_id:
                continue
            owner_id = model.objects.filter(pk=object_id).values_list("user_id", flat=True).first()
            if owner_id != self.user_id:
                errors[field_name] = "Related records must belong to the same user."
        if self.merged_into_id and self.merged_into_id == self.pk:
            errors["merged_into"] = "An observation cannot be merged into itself."
        if errors:
            raise ValidationError(errors)

    @property
    def is_open(self) -> bool:
        """Whether the observation still needs a reviewer decision."""

        return self.review_status == self.ReviewStatus.UNREVIEWED

    @property
    def feeds_reports(self) -> bool:
        """Whether this observation may contribute to financial reporting.

        Rejected and merged rows never do, which is what keeps a discarded
        candidate out of spending totals.
        """

        return (
            self.review_status in self.ACCEPTED_STATUSES
            and self.canonical_transaction_id is not None
        )

    def __str__(self) -> str:
        return f"{self.occurred_at or 'undated'} ({self.review_status})"
