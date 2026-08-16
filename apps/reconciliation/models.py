"""Proposed relationships between observations, and near-identical screenshots.

Reconciliation never deletes anything. When two rows look like the same real
event — the card's view and the bank's view of one purchase, a duplicate
screenshot, a card settlement — a *candidate* is recorded and a human decides.
Both observations survive either way, so the evidence for the decision is still
there afterwards.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.observations.models import ImportedObservation
from apps.processing.models import SourceDocument


class ReconciliationMatch(models.Model):
    """One proposed relationship between two observations."""

    class MatchType(models.TextChoices):
        DUPLICATE_OBSERVATION = "duplicate_observation", "Duplicate observation"
        DEBIT_CARD_BANK_MATCH = "debit_card_bank_match", "Debit-card and bank match"
        CREDIT_CARD_PAYMENT = "credit_card_payment", "Credit-card payment"
        INTERNAL_TRANSFER = "internal_transfer", "Internal transfer"
        REFUND_MATCH = "refund_match", "Refund match"
        STATEMENT_MEMBERSHIP = "statement_membership", "Statement membership"

    class Status(models.TextChoices):
        #: Detected automatically and waiting for a person.
        PROPOSED = "proposed", "Proposed"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"

    class Strength(models.TextChoices):
        """How the score should be presented, per specification 16.3."""

        LIKELY_MERGE = "likely_merge", "Propose merge"
        REVIEW_CANDIDATE = "review_candidate", "Likely duplicate"
        WEAK = "weak", "Keep separate"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reconciliation_matches",
    )
    left_observation = models.ForeignKey(
        ImportedObservation,
        on_delete=models.CASCADE,
        related_name="left_matches",
    )
    right_observation = models.ForeignKey(
        ImportedObservation,
        on_delete=models.CASCADE,
        related_name="right_matches",
    )
    match_type = models.CharField(max_length=32, choices=MatchType.choices)
    match_score = models.PositiveSmallIntegerField(default=0)
    #: The features that produced the score, so a reviewer can be told *why*
    #: two rows were paired rather than just how confidently.
    match_features_json_encrypted = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROPOSED)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviewed_reconciliation_matches",
        blank=True,
        null=True,
    )
    reviewed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-match_score", "-created_at")
        constraints = [
            # One pair, one candidate per relationship kind. Re-running
            # detection updates rather than piling up duplicates of duplicates.
            models.UniqueConstraint(
                fields=("left_observation", "right_observation", "match_type"),
                name="reconciliation_pair_unique",
            ),
            models.CheckConstraint(
                condition=~models.Q(left_observation=models.F("right_observation")),
                name="reconciliation_distinct_observations",
            ),
            models.CheckConstraint(
                condition=models.Q(match_score__lte=100),
                name="reconciliation_score_range",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "status", "-match_score"), name="match_user_status_idx"),
            models.Index(fields=("user", "match_type"), name="match_user_type_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.left_observation_id == self.right_observation_id:
            errors["right_observation"] = "A match must join two different observations."
        for name, observation_id in (
            ("left_observation", self.left_observation_id),
            ("right_observation", self.right_observation_id),
        ):
            if not observation_id:
                continue
            owner_id = (
                ImportedObservation.objects.filter(pk=observation_id)
                .values_list("user_id", flat=True)
                .first()
            )
            if owner_id != self.user_id:
                errors[name] = "Both observations must belong to the same user."
        if errors:
            raise ValidationError(errors)

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.PROPOSED

    def __str__(self) -> str:
        return f"{self.match_type} ({self.match_score})"


class NearDuplicateDocument(models.Model):
    """Two screenshots that differ only by crop, compression, or small UI changes.

    Kept apart from :class:`ReconciliationMatch` on purpose: two images looking
    alike is a statement about pixels, not about money, and must never be
    presented as though a transaction had been matched.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="near_duplicate_documents",
    )
    document = models.ForeignKey(
        SourceDocument,
        on_delete=models.CASCADE,
        related_name="near_duplicate_links",
    )
    similar_document = models.ForeignKey(
        SourceDocument,
        on_delete=models.CASCADE,
        related_name="near_duplicate_backlinks",
    )
    #: Hamming distance between the two perceptual hashes. Zero means the
    #: hashes are identical, which is still not proof the files are.
    distance = models.PositiveSmallIntegerField()
    algorithm = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("distance", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("document", "similar_document"),
                name="near_duplicate_pair_unique",
            ),
            models.CheckConstraint(
                condition=~models.Q(document=models.F("similar_document")),
                name="near_duplicate_distinct_documents",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "distance"), name="near_duplicate_user_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        if self.document_id == self.similar_document_id:
            raise ValidationError(
                {"similar_document": "A document cannot be a near duplicate of itself."}
            )
        owners: set[Any] = set()
        for document_id in (self.document_id, self.similar_document_id):
            if not document_id:
                continue
            owners.add(
                SourceDocument.objects.filter(pk=document_id)
                .values_list("user_id", flat=True)
                .first()
            )
        if owners and owners != {self.user_id}:
            raise ValidationError({"document": "Both documents must belong to the same user."})

    def __str__(self) -> str:
        return f"near duplicate (distance {self.distance})"
