"""Turn parser output into stored observations, atomically and idempotently.

The import is the boundary between the parsing layer, which deals in
dataclasses, and the database, which deals in encrypted rows. Two properties
matter most:

* **Atomic.** Either every row of a parse lands or none does, so review never
  sees half a screenshot.
* **Idempotent.** The same OCR run parsed by the same parser version cannot
  create a second set of rows, so a retried worker is harmless.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError
from django.db import transaction as db_transaction

from apps.categorization.normalization import merchant_blind_index, normalize_merchant
from apps.core.audit import record_audit_event
from apps.core.crypto import encrypt_model_field
from apps.core.errors import ConflictError, InvalidRequestError
from apps.core.value_objects import Currency, Money
from apps.ocr.models import OcrRun
from apps.parsing.contracts import ParsedObservation, TransactionDirection
from apps.parsing.registry import ParserSelection
from apps.processing.models import SourceDocument

from .models import ImportedObservation
from .risk import projections, score_flags

#: Confidence factors that name a review concern rather than a measurement.
FLAG_FACTORS = ("requires_review",)


class ObservationImportError(InvalidRequestError):
    """Parser output could not be stored as observations."""


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What one import attempt did."""

    observations: tuple[ImportedObservation, ...]
    created: bool
    parser_name: str
    parser_version: str

    @property
    def count(self) -> int:
        return len(self.observations)


def _direction(value: TransactionDirection | None) -> str:
    if value is None:
        return ImportedObservation.Direction.UNKNOWN
    return {
        TransactionDirection.DEBIT: ImportedObservation.Direction.DEBIT,
        TransactionDirection.CREDIT: ImportedObservation.Direction.CREDIT,
    }.get(value, ImportedObservation.Direction.UNKNOWN)


def _money_plaintext(observation: ParsedObservation) -> str:
    """Encode an amount as ``minor_units:CURRENCY`` before encryption."""

    minor = observation.amount_minor
    if minor is None or not observation.currency:
        return ""
    return f"{minor}:{Currency(observation.currency).code}"


def _balance_plaintext(observation: ParsedObservation) -> str:
    if observation.balance_after is None or not observation.currency:
        return ""
    currency = Currency(observation.currency)
    return f"{Money.from_decimal(observation.balance_after, currency).amount_minor}:{currency.code}"


def _source_region(observation: ParsedObservation) -> str:
    region = observation.source_region
    if region is None:
        return ""
    return json.dumps(
        {
            "left": region.left,
            "top": region.top,
            "right": region.right,
            "bottom": region.bottom,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _review_flags(observation: ParsedObservation) -> list[str]:
    """Names of the concerns this row carries. Never values."""

    flags = [f"missing_{name}" for name in sorted(observation.missing_fields)]
    flags.extend(f"ambiguous_{name}" for name in sorted(observation.ambiguous_fields))
    if observation.direction is None or observation.direction is TransactionDirection.UNKNOWN:
        flags.append("unknown_direction")
    balance_status = observation.confidence_factors.get("balance_status")
    if balance_status == "invalid":
        flags.append("balance_mismatch")
    if observation.confidence_factors.get("institution_fallback"):
        flags.append("parser_fallback")
    if observation.confidence_factors.get("parser_error"):
        flags.append("parser_error")
    if observation.is_settlement:
        flags.append("settlement_candidate")
    return sorted(set(flags))


def _confidences(observation: ParsedObservation) -> tuple[float, float, float]:
    """Split OCR confidence from parser confidence, then combine them.

    They are stored separately because they fail differently: a blurry image
    lowers the first, a layout the parser does not understand lowers the second.
    """

    factors = observation.confidence_factors
    ocr = _as_float(factors.get("token_confidence"))
    field_scores = [
        _as_float(factors.get(name))
        for name in ("date_confidence", "amount_confidence", "direction_confidence")
        if name in factors
    ]
    parser = sum(field_scores) / len(field_scores) if field_scores else 0.0
    if not field_scores:
        # The generic parser reports no per-field scores; fall back to its
        # support score, which the registry stamps onto every row.
        parser = observation.parser_support_score
    overall = min(ocr, parser) if ocr and parser else max(ocr, parser)
    return _clamp(ocr), _clamp(parser), _clamp(overall)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _build(
    *,
    document: SourceDocument,
    ocr_run: OcrRun | None,
    parsed: ParsedObservation,
    row_index: int,
) -> ImportedObservation:
    ocr_confidence, parser_confidence, overall = _confidences(parsed)
    flags = _review_flags(parsed)
    # A freshly imported row has no account or card mapping yet, so the unknown
    # mapping penalty always applies; re-scoring happens when review maps it.
    risk_score, _ = score_flags(flags, overall_confidence=overall, has_mapping=False)
    return ImportedObservation(
        **projections(flags),
        risk_score=risk_score,
        user_id=document.user_id,
        source_document=document,
        ocr_run=ocr_run,
        row_index=row_index,
        occurred_at=parsed.occurred_on,
        currency=(parsed.currency or "").upper(),
        direction=_direction(parsed.direction),
        installment_months=parsed.installment_months,
        ocr_confidence=ocr_confidence,
        parser_confidence=parser_confidence,
        overall_confidence=overall,
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        parser_output_version=parsed.output_version,
        review_flags=flags,
        requires_review=bool(flags) or bool(parsed.confidence_factors.get("requires_review")),
    )


def _encrypt_fields(
    record: ImportedObservation,
    parsed: ParsedObservation,
    *,
    data_key: bytes,
    key_version: int,
    blind_index_key: bytes | None = None,
) -> None:
    """Encrypt every value-bearing field once the record has an identity.

    The merchant is stored twice on purpose: the raw text as the source printed
    it, which is what a person is shown, and a normalized form that exists only
    to be looked up. Overwriting the first with the second would tell the user
    their coffee came from a shop whose name they have never seen.
    """

    normalized = ""
    if parsed.merchant:
        try:
            normalized = normalize_merchant(parsed.merchant)
        except InvalidRequestError:
            # A merchant that normalizes to nothing is not a lookup key. The
            # raw text is still kept; only the index is skipped.
            normalized = ""
    if normalized and blind_index_key is not None:
        record.merchant_blind_index = merchant_blind_index(
            parsed.merchant or "", user_id=record.user_id, key=blind_index_key
        )

    plaintexts = {
        "merchant_raw_encrypted": parsed.merchant or "",
        "merchant_normalized_encrypted": normalized,
        "amount_encrypted": _money_plaintext(parsed),
        "balance_after_encrypted": _balance_plaintext(parsed),
        "approval_code_encrypted": parsed.approval_code or "",
        "source_region_json_encrypted": _source_region(parsed),
    }
    for field, plaintext in plaintexts.items():
        if not plaintext:
            continue
        setattr(
            record,
            field,
            encrypt_model_field(record, field, plaintext, key=data_key, key_version=key_version),
        )


def existing_import(
    *, ocr_run: OcrRun, parser_name: str, parser_version: str
) -> tuple[ImportedObservation, ...]:
    """Observations already imported for one run and parser version."""

    return tuple(
        ImportedObservation.objects.filter(
            ocr_run=ocr_run, parser_name=parser_name, parser_version=parser_version
        ).order_by("row_index")
    )


@db_transaction.atomic
def import_parser_selection(
    *,
    document: SourceDocument,
    ocr_run: OcrRun | None,
    selection: ParserSelection,
    data_key: bytes,
    key_version: int,
    blind_index_key: bytes | None = None,
    actor: Any | None = None,
) -> ImportResult:
    """Store one parse as observations, exactly once.

    Rolling back is confined to this transaction: a failure leaves earlier
    imports, the OCR runs, and every canonical transaction untouched.
    """

    parser_name = selection.metadata.name
    parser_version = selection.metadata.version
    if ocr_run is not None and ocr_run.user_id != document.user_id:
        raise ObservationImportError("The OCR run does not belong to the document owner.")

    if ocr_run is not None:
        already = existing_import(
            ocr_run=ocr_run, parser_name=parser_name, parser_version=parser_version
        )
        if already:
            return ImportResult(already, False, parser_name, parser_version)

    records: list[ImportedObservation] = []
    # Parser order is the screen's reading order, so the row index it yields is
    # also the order review should present.
    for row_index, parsed in enumerate(selection.observations):
        record = _build(document=document, ocr_run=ocr_run, parsed=parsed, row_index=row_index)
        record.full_clean(exclude=["merged_into"])
        _encrypt_fields(
            record,
            parsed,
            data_key=data_key,
            key_version=key_version,
            blind_index_key=blind_index_key,
        )
        records.append(record)

    try:
        created = ImportedObservation.objects.bulk_create(records)
    except IntegrityError as exc:
        raise ConflictError("These observations were already imported for this OCR run.") from exc

    record_audit_event(
        user=actor if actor is not None else document.user,
        event_type="observations_imported",
        obj=document,
        metadata={
            "parser": parser_name,
            "parser_version": parser_version,
            "observation_count": len(created),
            "ocr_run_id": str(ocr_run.pk) if ocr_run is not None else "",
        },
    )
    return ImportResult(tuple(created), True, parser_name, parser_version)


def rescore_observation(observation: ImportedObservation, *, save: bool = True) -> int:
    """Recompute a row's stored risk from its current state.

    Risk is first scored at import, when nothing is mapped yet. Once review
    assigns an account or card, or corrects a flagged field, the stored score is
    stale and the queue would keep ranking the row as though it were still
    blocked — so every mutation path calls this.
    """

    flags = [str(flag) for flag in observation.review_flags or ()]
    has_mapping = (
        observation.financial_account_guess_id is not None
        or observation.payment_instrument_guess_id is not None
    )
    score, _ = score_flags(
        flags, overall_confidence=observation.overall_confidence, has_mapping=has_mapping
    )
    updates = projections(flags)
    for name, value in updates.items():
        setattr(observation, name, value)
    observation.risk_score = score
    if save:
        observation.save(update_fields=[*updates, "risk_score", "updated_at"])
    return score


def observations_for_document(
    document: SourceDocument, *, statuses: Sequence[str] | None = None
) -> Any:
    """Every observation of one document, in screen order."""

    queryset = ImportedObservation.objects.filter(source_document=document)
    if statuses is not None:
        queryset = queryset.filter(review_status__in=list(statuses))
    return queryset.order_by("row_index", "created_at")
