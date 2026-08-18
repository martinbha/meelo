"""Letting a reviewer say what a screenshot actually is.

Detection reads the pixels and is sometimes wrong: a Kakao Bank transfer receipt
that says "bank transaction list", a card statement read as a card list. The
consequence is not cosmetic — the wrong parser produces the wrong rows, or none.

So a reviewer can state the answer, and the next pass uses it. The override is
stored beside the guess rather than on top of it, for two reasons. Detection
accuracy is measured by comparing the two, which is impossible once the guess
has been overwritten. And a reviewer who was wrong has to be able to get back to
automatic behaviour, which is impossible once the original guess is gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction as db_transaction

from apps.core.audit import record_audit_event
from apps.core.errors import ForbiddenError, InvalidRequestError
from apps.parsing.institutions import INSTITUTION_PARSER_CLASSES

from .models import SourceDocument


class OverrideError(InvalidRequestError):
    """The requested override is not one this system can honour."""


def institution_choices() -> tuple[tuple[str, str], ...]:
    """The institutions a reviewer may choose, as ``(name, display name)``.

    Read from the registered parsers rather than written out again here. A list
    that has to be kept in step with the registry by hand is a list that offers
    a parser which no longer exists, and the reviewer finds out only when the
    next pass quietly falls back to detection.
    """

    return tuple(
        (parser_class.profile.name, parser_class.profile.display_name)
        for parser_class in INSTITUTION_PARSER_CLASSES
    )


def institution_names() -> frozenset[str]:
    return frozenset(name for name, _ in institution_choices())


@dataclass(frozen=True, slots=True)
class OverrideChange:
    """What one call changed, for the caller's message and the audit record."""

    document: SourceDocument
    previous_source_type: str
    previous_institution: str

    @property
    def cleared(self) -> bool:
        """Whether this call removed the last override the document had."""

        return not self.document.has_overrides and bool(
            self.previous_source_type or self.previous_institution
        )

    @property
    def changed(self) -> bool:
        return (
            self.previous_source_type != self.document.source_type_override
            or self.previous_institution != self.document.institution_override
        )


def _validated_source_type(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    if normalized not in SourceDocument.SourceType.values:
        raise OverrideError(f"'{normalized}' is not a source type this system recognises.")
    return normalized


def _validated_institution(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    if normalized not in institution_names():
        raise OverrideError(f"No parser named '{normalized}' is registered.")
    return normalized


@db_transaction.atomic
def set_document_overrides(
    document_id: Any,
    *,
    user: Any,
    source_type: str = "",
    institution: str = "",
) -> OverrideChange:
    """Record what a reviewer says this document is. Empty values clear.

    Both overrides are set in one call rather than one each, so "source type
    only" cannot leave a stale institution behind. The document is looked up
    through the owner filter and locked, so an override can only ever reach the
    row of the person setting it — an override is an instruction to a parser
    that will later decrypt this user's data, and pointing it at someone else's
    document is exactly the thing ownership scoping exists to prevent.
    """

    resolved_source_type = _validated_source_type(source_type)
    resolved_institution = _validated_institution(institution)

    document = (
        SourceDocument.objects.select_for_update().filter(pk=document_id, user_id=user.pk).first()
    )
    if document is None:
        raise ForbiddenError("This document belongs to another user.")

    previous_source_type = document.source_type_override
    previous_institution = document.institution_override
    if (previous_source_type, previous_institution) == (
        resolved_source_type,
        resolved_institution,
    ):
        # Nothing to write and nothing to record. A reviewer who submits the
        # form twice has not made a second decision.
        return OverrideChange(document, previous_source_type, previous_institution)

    document.source_type_override = resolved_source_type
    document.institution_override = resolved_institution
    document.save(update_fields=["source_type_override", "institution_override"])
    change = OverrideChange(document, previous_source_type, previous_institution)

    record_audit_event(
        user=user,
        event_type=("document_override_cleared" if change.cleared else "document_override_set"),
        obj=document,
        # Source types and parser names are a closed vocabulary that says
        # nothing about the person or their money, so they are recorded in
        # clear. The screenshot's contents never are.
        metadata={
            "source_type_override": resolved_source_type,
            "institution_override": resolved_institution,
            "previous_source_type_override": change.previous_source_type,
            "previous_institution_override": change.previous_institution,
            "detected_source_type": document.source_type,
        },
    )
    return change
