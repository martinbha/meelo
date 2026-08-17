"""Reviewer decisions: correct, accept, reject, merge, and reprocess.

Nothing here is implicit. An observation becomes financial history only when a
reviewer says so, every decision is audited, and every path refuses to touch a
row that belongs to somebody else.

Acceptance is the sharp edge: it creates the canonical transaction and, when
ledger accounts are supplied, posts the entries — all inside one transaction, so
a row can never end up accepted without its posting, or posted twice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.utils import timezone

from apps.categorization.engine import CategorySource
from apps.categorization.models import Category
from apps.core.audit import record_audit_event
from apps.core.crypto import decrypt_model_field, encrypt_model_field
from apps.core.errors import ConflictError, ForbiddenError, InvalidRequestError
from apps.core.value_objects import Currency, InvalidCurrencyError, Money
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument
from apps.ledger.models import LedgerEntry
from apps.ledger.rules import PostingRuleAccounts, post_transaction_by_type
from apps.transactions.idempotency import OBSERVATION_SOURCE, save_once, source_key
from apps.transactions.models import CanonicalTransaction

from .models import ImportedObservation
from .risk import HIGH_RISK_THRESHOLD
from .services import rescore_observation

#: Fields a reviewer may correct. Anything outside this set is rejected rather
#: than silently ignored, so a typo in a form name cannot lose a correction.
CORRECTABLE_FIELDS = frozenset(
    {
        "occurred_at",
        "posted_at",
        "merchant",
        "amount_minor",
        "currency",
        "direction",
        "transaction_type_guess",
        "financial_account_guess",
        "payment_instrument_guess",
        "category_guess",
        "installment_months",
    }
)

#: Order corrections are applied in. Currency comes before the amount so an
#: amount corrected in the same request is encoded in the corrected currency.
CORRECTION_ORDER = (
    "currency",
    "amount_minor",
    "occurred_at",
    "posted_at",
    "merchant",
    "direction",
    "transaction_type_guess",
    "financial_account_guess",
    "payment_instrument_guess",
    "category_guess",
    "installment_months",
)

#: Flags cleared once the reviewer supplies the field they complained about.
FLAGS_CLEARED_BY: Mapping[str, tuple[str, ...]] = {
    "occurred_at": ("missing_date", "ambiguous_date"),
    "merchant": ("missing_merchant", "ambiguous_merchant"),
    "amount_minor": ("missing_amount", "ambiguous_amount"),
    "direction": ("missing_direction", "unknown_direction"),
}


class ObservationActionError(InvalidRequestError):
    """A review action cannot be applied to this observation."""


@dataclass(frozen=True, slots=True)
class DecryptedObservation:
    """An observation with its values readable, for display and comparison."""

    observation: ImportedObservation
    merchant: str
    amount: Money | None
    balance_after: Money | None
    approval_code: str
    source_region: str

    @property
    def amount_minor(self) -> int | None:
        return self.amount.amount_minor if self.amount is not None else None


def _decrypt(observation: ImportedObservation, field: str, *, data_key: bytes) -> str:
    if not getattr(observation, field):
        return ""
    return decrypt_model_field(observation, field, key=data_key)


def _money_or_none(value: str) -> Money | None:
    if not value:
        return None
    minor, _, currency = value.partition(":")
    try:
        return Money(int(minor), currency)
    except (TypeError, ValueError, InvalidCurrencyError):
        return None


def decrypt_observation(
    observation: ImportedObservation, *, user: Any, data_key: bytes
) -> DecryptedObservation:
    """Read one observation's encrypted values, for its owner only."""

    assert_reviewable(observation, user)
    return DecryptedObservation(
        observation=observation,
        merchant=_decrypt(observation, "merchant_raw_encrypted", data_key=data_key),
        amount=_money_or_none(_decrypt(observation, "amount_encrypted", data_key=data_key)),
        balance_after=_money_or_none(
            _decrypt(observation, "balance_after_encrypted", data_key=data_key)
        ),
        approval_code=_decrypt(observation, "approval_code_encrypted", data_key=data_key),
        source_region=_decrypt(observation, "source_region_json_encrypted", data_key=data_key),
    )


def assert_reviewable(observation: ImportedObservation, user: Any) -> ImportedObservation:
    """Refuse to act on an observation the requesting user does not own."""

    if not getattr(user, "is_authenticated", False) or observation.user_id != user.pk:
        raise ForbiddenError("This observation belongs to another user.")
    return observation


def _lock(observation_id: Any, user: Any) -> ImportedObservation:
    locked = (
        ImportedObservation.objects.select_for_update()
        .filter(pk=observation_id, user_id=user.pk)
        .first()
    )
    if locked is None:
        raise ForbiddenError("This observation belongs to another user.")
    return locked


def _owned(model: Any, value: Any, user: Any, label: str) -> Any:
    if value is None:
        return None
    if value.user_id != user.pk:
        raise ObservationActionError(f"The {label} does not belong to this user.")
    return value


def _clear_flags(observation: ImportedObservation, field: str) -> None:
    removable = set(FLAGS_CLEARED_BY.get(field, ()))
    if not removable:
        return
    observation.review_flags = [
        flag for flag in observation.review_flags or () if str(flag) not in removable
    ]


@db_transaction.atomic
def correct_observation(
    observation_id: Any,
    *,
    user: Any,
    data_key: bytes,
    key_version: int,
    corrections: Mapping[str, Any],
) -> ImportedObservation:
    """Apply reviewer corrections, keeping a record of what was changed.

    The parser's original values stay in the audit trail through the field
    names recorded on the row; an invalid correction is refused outright rather
    than partially applied.
    """

    observation = _lock(observation_id, user)
    if observation.review_status in ImportedObservation.RESOLVED_STATUSES:
        raise ConflictError("Rejected or merged observations cannot be corrected.")

    unknown = set(corrections) - CORRECTABLE_FIELDS
    if unknown:
        raise ObservationActionError(f"Unknown correctable fields: {', '.join(sorted(unknown))}.")

    changed: list[str] = []
    # Currency is applied first so that a same-call amount correction is encoded
    # in the corrected currency rather than the one being replaced.
    for field in sorted(corrections, key=lambda name: CORRECTION_ORDER.index(name)):
        if _apply_correction(
            observation,
            field,
            corrections[field],
            user=user,
            data_key=data_key,
            key_version=key_version,
        ):
            changed.append(field)
            _clear_flags(observation, field)

    if not changed:
        return observation

    observation.corrected_fields = sorted(set(observation.corrected_fields or []) | set(changed))
    observation.review_status = ImportedObservation.ReviewStatus.CORRECTED
    observation.reviewed_by = user
    observation.reviewed_at = timezone.now()
    observation.full_clean(exclude=["merged_into"])
    observation.save()
    rescore_observation(observation)

    record_audit_event(
        user=user,
        event_type="observation_corrected",
        obj=observation,
        # Field names only: an audit log must never become a second copy of the
        # financial data it describes.
        metadata={
            "fields": sorted(changed),
            "source_document_id": str(observation.source_document_id),
        },
    )
    return observation


def _apply_correction(
    observation: ImportedObservation,
    field: str,
    value: Any,
    *,
    user: Any,
    data_key: bytes,
    key_version: int,
) -> bool:
    """Set one field, returning whether it actually changed."""

    if field == "merchant":
        text = str(value or "")
        current = _decrypt(observation, "merchant_raw_encrypted", data_key=data_key)
        if text == current:
            return False
        observation.merchant_raw_encrypted = (
            encrypt_model_field(
                observation, "merchant_raw_encrypted", text, key=data_key, key_version=key_version
            )
            if text
            else ""
        )
        return True

    if field == "amount_minor":
        if value is None:
            raise ObservationActionError("An amount correction cannot be empty.")
        if not observation.currency:
            # Guessing a currency here would post a real amount in the wrong
            # one. Corrections apply currency first, so submitting both works.
            raise ObservationActionError("Set the currency before correcting the amount.")
        currency_code = str(observation.currency)
        try:
            money = Money(int(value), currency_code)
        except (TypeError, ValueError, InvalidCurrencyError) as exc:
            raise ObservationActionError("Amounts must be whole minor units.") from exc
        if money.amount_minor <= 0:
            raise ObservationActionError("Amounts must be greater than zero.")
        plaintext = f"{money.amount_minor}:{money.resolved_currency.code}"
        if plaintext == _decrypt(observation, "amount_encrypted", data_key=data_key):
            return False
        observation.amount_encrypted = encrypt_model_field(
            observation, "amount_encrypted", plaintext, key=data_key, key_version=key_version
        )
        return True

    if field == "currency":
        try:
            code = Currency(str(value)).code
        except InvalidCurrencyError as exc:
            raise ObservationActionError("Currency must be a three-letter code.") from exc
        if code == observation.currency:
            return False
        observation.currency = code
        # The encrypted amount carries its own currency suffix. Leaving it on
        # the old code would make acceptance post in a currency the reviewer
        # just corrected away from.
        existing = _money_or_none(_decrypt(observation, "amount_encrypted", data_key=data_key))
        if existing is not None:
            observation.amount_encrypted = encrypt_model_field(
                observation,
                "amount_encrypted",
                f"{existing.amount_minor}:{code}",
                key=data_key,
                key_version=key_version,
            )
        return True

    if field in {"occurred_at", "posted_at"}:
        if value is not None and not isinstance(value, date):
            raise ObservationActionError(f"{field} must be a date.")
        if getattr(observation, field) == value:
            return False
        setattr(observation, field, value)
        return True

    if field == "direction":
        if value not in ImportedObservation.Direction.values:
            raise ObservationActionError("Direction must be debit, credit, or unknown.")
        if observation.direction == value:
            return False
        observation.direction = value
        return True

    if field == "transaction_type_guess":
        if value not in CanonicalTransaction.TransactionType.values:
            raise ObservationActionError("Unknown transaction type.")
        if observation.transaction_type_guess == value:
            return False
        observation.transaction_type_guess = value
        return True

    if field == "installment_months":
        if value is not None and (not isinstance(value, int) or value < 1):
            raise ObservationActionError("Installment counts must be at least one month.")
        if observation.installment_months == value:
            return False
        observation.installment_months = value
        return True

    models: Mapping[str, tuple[Any, str]] = {
        "financial_account_guess": (FinancialAccount, "financial account"),
        "payment_instrument_guess": (PaymentInstrument, "payment instrument"),
        "category_guess": (Category, "category"),
    }
    model, label = models[field]
    resolved = _owned(model, value, user, label)
    if getattr(observation, f"{field}_id") == (resolved.pk if resolved else None):
        return False
    setattr(observation, field, resolved)
    return True


def _link_accepted(
    observation: ImportedObservation, canonical: CanonicalTransaction, *, user: Any
) -> None:
    """Point a reviewed row at the transaction it produced."""

    observation.canonical_transaction = canonical
    observation.review_status = (
        ImportedObservation.ReviewStatus.CORRECTED
        if observation.corrected_fields
        else ImportedObservation.ReviewStatus.ACCEPTED
    )
    observation.reviewed_by = user
    observation.reviewed_at = timezone.now()
    observation.save(
        update_fields=[
            "canonical_transaction",
            "review_status",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ]
    )


def _requires_confirmation(observation: ImportedObservation) -> bool:
    """Whether accepting this row should demand a second, explicit confirmation."""

    return observation.risk_score >= HIGH_RISK_THRESHOLD or observation.amount_uncertain


@db_transaction.atomic
def accept_observation(
    observation_id: Any,
    *,
    user: Any,
    data_key: bytes,
    financial_account: FinancialAccount | None = None,
    transaction_type: str | None = None,
    ledger_accounts: PostingRuleAccounts | None = None,
    confirmed: bool = False,
) -> CanonicalTransaction:
    """Turn one reviewed observation into a canonical transaction.

    Repeating the call returns the transaction already created rather than
    making a second one, and the ledger posting happens in the same database
    transaction as the status change.
    """

    observation = _lock(observation_id, user)
    existing = observation.canonical_transaction
    if existing is not None:
        # Idempotent: a retried click must not create a second transaction.
        return existing

    if observation.review_status in ImportedObservation.RESOLVED_STATUSES:
        raise ConflictError("Rejected or merged observations cannot be accepted.")
    if _requires_confirmation(observation) and not confirmed:
        raise ConflictError(
            "This observation is low confidence or has a disputed amount; "
            "confirm explicitly to accept it."
        )

    account = (
        _owned(FinancialAccount, financial_account, user, "financial account")
        or observation.financial_account_guess
    )
    if account is None:
        raise ObservationActionError("Accepting an observation requires a financial account.")

    amount = _money_or_none(_decrypt(observation, "amount_encrypted", data_key=data_key))
    if amount is None or amount.amount_minor <= 0:
        raise ObservationActionError("An observation without a usable amount cannot be accepted.")
    if observation.occurred_at is None:
        raise ObservationActionError("An observation without a date cannot be accepted.")

    resolved_type = transaction_type or observation.transaction_type_guess
    if resolved_type not in CanonicalTransaction.TransactionType.values:
        raise ObservationActionError("Unknown transaction type.")

    instrument = observation.payment_instrument_guess
    if instrument is not None and instrument.financial_account_id != account.pk:
        # The manual creation path enforces this too. A card attached to a
        # different account would post entries against the wrong balance.
        raise ObservationActionError(
            "The payment instrument is not compatible with the selected account."
        )

    canonical = CanonicalTransaction(
        user_id=user.pk,
        created_by=user,
        reviewed_by=user,
        occurred_at=observation.occurred_at,
        posted_at=observation.posted_at,
        amount_encrypted=f"{amount.amount_minor}:{amount.resolved_currency.code}",
        currency=amount.resolved_currency.code,
        transaction_type=resolved_type,
        financial_account=account,
        payment_instrument=observation.payment_instrument_guess,
        category=observation.category_guess,
        # A guess carried over from the parser is recorded as one, so a report
        # asking for uncategorised rows does not count a row that has a
        # category, and so the priority engine knows how weak the evidence is.
        category_source=(
            CategorySource.PARSER
            if observation.category_guess_id is not None
            else CategorySource.UNCATEGORIZED
        ),
        merchant_encrypted=_decrypt(observation, "merchant_raw_encrypted", data_key=data_key),
        status=CanonicalTransaction.Status.DRAFT,
        source_idempotency_key=source_key(OBSERVATION_SOURCE, observation.pk),
    )
    try:
        # Constraint checks are left to the database: the idempotency key is
        # meant to collide when an attempt is repeated, and save_once resolves
        # that collision. Refusing it here would turn a converged retry into an
        # error the caller cannot act on.
        canonical.full_clean(validate_constraints=False)
    except ValidationError as exc:
        raise ObservationActionError(f"The canonical transaction is invalid: {exc}") from exc
    canonical, created = save_once(canonical)
    if created and ledger_accounts is not None:
        # Confirm before posting: the ledger only accepts confirmed rows, and a
        # failure here rolls back the acceptance with it. Skipped when another
        # attempt won the race — its transaction is the one that counts, and
        # posting against it again would double the entries.
        canonical.status = CanonicalTransaction.Status.CONFIRMED
        canonical.save(update_fields=["status", "updated_at"])
        post_transaction_by_type(canonical, ledger_accounts)

    _link_accepted(observation, canonical, user=user)

    record_audit_event(
        user=user,
        event_type="observation_accepted",
        obj=observation,
        metadata={
            "canonical_transaction_id": str(canonical.pk),
            # The winner's type, not this attempt's: they can differ, and the
            # log has to describe the transaction that actually exists.
            "transaction_type": canonical.transaction_type,
            "posted": created and ledger_accounts is not None,
            "converged": not created,
        },
    )
    return canonical


@db_transaction.atomic
def reject_observation(observation_id: Any, *, user: Any, reason: str = "") -> ImportedObservation:
    """Discard a candidate. It stays stored, but never reaches reporting."""

    observation = _lock(observation_id, user)
    if observation.canonical_transaction_id is not None:
        raise ConflictError("An accepted observation cannot be rejected; void the transaction.")
    if observation.review_status == ImportedObservation.ReviewStatus.REJECTED:
        return observation

    observation.review_status = ImportedObservation.ReviewStatus.REJECTED
    observation.reviewed_by = user
    observation.reviewed_at = timezone.now()
    observation.save(update_fields=["review_status", "reviewed_by", "reviewed_at", "updated_at"])
    record_audit_event(
        user=user,
        event_type="observation_rejected",
        obj=observation,
        # The reason is a short reviewer note; it is recorded as a length only
        # so free text can never smuggle values into the audit log.
        metadata={"reason_length": len(reason)},
    )
    return observation


@db_transaction.atomic
def merge_observations(
    *, user: Any, winner_id: Any, duplicate_ids: Sequence[Any]
) -> ImportedObservation:
    """Fold duplicate rows into one, keeping every source traceable.

    The duplicates are not deleted: each keeps its own source document and now
    points at the row that won, so the evidence for the merge survives.
    """

    winner = _lock(winner_id, user)
    if winner.merged_into_id is not None:
        # Merging into an already-merged row would build a chain nobody can
        # follow back to the surviving transaction.
        raise ConflictError("The winning observation was itself merged into another row.")
    merged: list[ImportedObservation] = []
    for duplicate_id in duplicate_ids:
        if duplicate_id == winner_id:
            raise ObservationActionError("An observation cannot be merged into itself.")
        duplicate = _lock(duplicate_id, user)
        if duplicate.canonical_transaction_id is not None:
            raise ConflictError(
                "An observation with a confirmed transaction cannot be merged away."
            )
        if duplicate.review_status == ImportedObservation.ReviewStatus.MERGED:
            if duplicate.merged_into_id == winner.pk:
                continue  # Idempotent: already merged into this winner.
            raise ConflictError("This observation was already merged into another row.")
        duplicate.merged_into = winner
        duplicate.review_status = ImportedObservation.ReviewStatus.MERGED
        duplicate.reviewed_by = user
        duplicate.reviewed_at = timezone.now()
        duplicate.save(
            update_fields=[
                "merged_into",
                "review_status",
                "reviewed_by",
                "reviewed_at",
                "updated_at",
            ]
        )
        merged.append(duplicate)

    if merged:
        record_audit_event(
            user=user,
            event_type="observation_merged",
            obj=winner,
            metadata={
                "merged_observation_ids": sorted(str(item.pk) for item in merged),
                "source_document_ids": sorted({str(item.source_document_id) for item in merged}),
            },
        )
    return winner


def has_posted_entries(canonical: CanonicalTransaction) -> bool:
    return LedgerEntry.objects.filter(transaction=canonical).exists()
