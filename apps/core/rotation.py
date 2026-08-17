"""Moving a user's encrypted values from one key to the next.

Rotation is a long operation over data a person cannot afford to lose, so the
design is shaped entirely by what happens when it stops halfway.

**Every envelope carries its key version.** That single fact is what makes this
resumable without a progress table: a row already sealed under the target version
is skipped, so re-running after a crash processes exactly what is left. There is
no cursor to corrupt and no bookkeeping that can disagree with the data.

**The new key becomes active first, then the values move.** The other order would
leave new writes landing under a key that is about to be retired, so the rotation
would chase its own tail. Once the new key is active, everything written from
that moment is already correct, and rotation only has to catch up with history.

**Rotation runs with the application stopped.** The new key becomes active
before the values move, so a row rotation has not reached yet cannot be read by a
request arriving in the meantime — its envelope is still sealed under the retired
key. That is a deliberate trade: the alternative leaves new writes landing under
the key being retired, which is a correctness problem rather than an availability
one. The operator's runbook says to stop the web and worker processes first.

**Nothing is retired until it has been read.** ``verify_user`` decrypts every
field under the active key and reports what failed. An old key deleted while one
row still needs it is not a degraded system, it is a row nobody can ever read
again (specification 22.6, 25.4).

Blind indexes are rebuilt in the same pass, because the search key is derived
from the data key: a rotated merchant whose index still came from the old key
would be unfindable, and nothing would say so.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction as db_transaction

from .crypto import (
    EncryptionError,
    encrypt_model_field,
    envelope_key_version,
    is_encrypted_value,
    read_model_field,
)

#: Rows handled per transaction. Bounded so a rotation over a large history does
#: not hold one lock for its entire duration, and so an interruption loses at
#: most this many rows' worth of work — which the next run redoes anyway.
DEFAULT_BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class EncryptedModel:
    """One model's encrypted fields, and the indexes derived from them."""

    label: str
    fields: tuple[str, ...]
    #: Blind-index columns rebuilt alongside, mapped to the field they index and
    #: the domain they belong to.
    indexes: tuple[tuple[str, str, str], ...] = ()
    #: How this model's rows are selected for one user. Most hold the owner
    #: directly; a ledger entry belongs to whoever owns its transaction, so it
    #: is reached through that.
    owner_filter: str = "user_id"

    @property
    def owns_itself(self) -> bool:
        """Whether the row carries its own owner, or borrows one for the AAD."""

        return self.owner_filter == "user_id"

    def model(self) -> Any:
        from django.apps import apps

        app_label, model_name = self.label.split(".")
        return apps.get_model(app_label, model_name)


#: Every model holding encrypted values, and which columns those are. Kept in one
#: place because the failure it prevents is silent: a field missing from this
#: list is a field rotation walks past, leaving a row readable only by a key that
#: is about to be deleted.
ENCRYPTED_MODELS: tuple[EncryptedModel, ...] = (
    EncryptedModel(
        "transactions.CanonicalTransaction",
        (
            "amount_encrypted",
            "merchant_encrypted",
            "counterparty_encrypted",
            "notes_encrypted",
        ),
        (("merchant_blind_index", "merchant_encrypted", "merchant"),),
    ),
    EncryptedModel(
        "observations.ImportedObservation",
        (
            "merchant_raw_encrypted",
            "merchant_normalized_encrypted",
            "counterparty_raw_encrypted",
            "amount_encrypted",
            "balance_after_encrypted",
            "approval_code_encrypted",
            "source_region_json_encrypted",
        ),
        (("merchant_blind_index", "merchant_raw_encrypted", "merchant"),),
    ),
    EncryptedModel(
        "categorization.Category",
        ("name_encrypted",),
    ),
    EncryptedModel(
        "categorization.MerchantAlias",
        ("alias_encrypted", "normalized_merchant_encrypted"),
        (
            ("alias_blind_index", "alias_encrypted", "merchant"),
            (
                "normalized_merchant_blind_index",
                "normalized_merchant_encrypted",
                "merchant",
            ),
        ),
    ),
    EncryptedModel(
        "categorization.CategoryRule",
        ("merchant_pattern_encrypted", "amount_min_encrypted", "amount_max_encrypted"),
        (("merchant_pattern_blind_index", "merchant_pattern_encrypted", "merchant"),),
    ),
    EncryptedModel(
        "financial_accounts.FinancialAccount",
        (
            "name_encrypted",
            "institution_encrypted",
            "masked_identifier_encrypted",
            "opening_balance_encrypted",
        ),
    ),
    EncryptedModel(
        "instruments.PaymentInstrument",
        ("name_encrypted", "issuer_encrypted"),
    ),
    EncryptedModel("ledger.ChartOfAccounts", ("name_encrypted",)),
    EncryptedModel("ledger.LedgerAccount", ("name_encrypted",)),
    # An entry has no owner column: its associated data borrows the transaction's
    # owner, so rotation reaches it the same way and supplies the same identifier.
    EncryptedModel(
        "ledger.LedgerEntry",
        ("amount_encrypted",),
        owner_filter="transaction__user_id",
    ),
    EncryptedModel(
        "ocr.OcrRun", ("configuration_encrypted", "preprocessing_encrypted", "raw_output_encrypted")
    ),
    EncryptedModel("ocr.OcrToken", ("text_encrypted", "normalized_text_encrypted")),
    EncryptedModel("reconciliation.ReconciliationMatch", ("match_features_json_encrypted",)),
    EncryptedModel(
        "processing.SourceDocument",
        (
            "original_filename_encrypted",
            "source_institution_guess_encrypted",
            "error_message_encrypted",
        ),
    ),
)


@dataclass
class RotationReport:
    """What one rotation did, and what it could not do."""

    rows_examined: int = 0
    rows_rewritten: int = 0
    fields_rewritten: int = 0
    indexes_rebuilt: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.failures

    def merge(self, other: RotationReport) -> None:
        self.rows_examined += other.rows_examined
        self.rows_rewritten += other.rows_rewritten
        self.fields_rewritten += other.fields_rewritten
        self.indexes_rebuilt += other.indexes_rebuilt
        self.failures.extend(other.failures)


def _batches(queryset: Any, size: int) -> Iterator[list[Any]]:
    """Walk a queryset in bounded chunks, holding one chunk at a time.

    A keyset cursor rather than a slice or a list of every identifier: this runs
    over a whole financial history, and "bounded batches" has to mean bounded
    *memory* as well as bounded transactions. Ordering by primary key also makes
    the walk stable across the writes rotation is doing to the same rows.
    """

    cursor: Any = None
    while True:
        window = queryset.order_by("pk")
        if cursor is not None:
            window = window.filter(pk__gt=cursor)
        batch = list(window[:size])
        if not batch:
            return
        yield batch
        cursor = batch[-1].pk


def _rebuild_indexes(
    record: Any,
    spec: EncryptedModel,
    *,
    plaintexts: dict[str, str],
    search_key: bytes,
) -> int:
    """Rebuild the blind indexes derived from fields this row just moved."""

    from apps.categorization.normalization import merchant_blind_index

    rebuilt = 0
    for column, source_field, domain in spec.indexes:
        value = plaintexts.get(source_field, "")
        if not value:
            continue
        if domain != "merchant":  # pragma: no cover - only merchant domains exist yet
            continue
        setattr(
            record,
            column,
            merchant_blind_index(value, user_id=record.user_id, key=search_key),
        )
        rebuilt += 1
    return rebuilt


def rotate_model(
    spec: EncryptedModel,
    *,
    user: Any,
    old_key: bytes,
    new_key: bytes,
    new_version: int,
    search_key: bytes,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> RotationReport:
    """Move one model's rows for one user onto the new key.

    Rows already at the target version are skipped, which is what makes a
    re-run after an interruption cheap and correct rather than a second full
    pass.
    """

    report = RotationReport()
    owner_id = None if spec.owns_itself else user.pk
    queryset = spec.model().objects.filter(**{spec.owner_filter: user.pk})
    for batch in _batches(queryset, batch_size):
        with db_transaction.atomic():
            for record in batch:
                report.rows_examined += 1
                changed: list[str] = []
                plaintexts: dict[str, str] = {}
                for name in spec.fields:
                    stored = getattr(record, name) or ""
                    if not stored:
                        continue
                    version = envelope_key_version(stored)
                    if version == new_version:
                        # Already moved, by an earlier run or by a write that
                        # happened after the new key became active.
                        if is_encrypted_value(stored):
                            plaintexts[name] = read_model_field(
                                record, name, key=new_key, user_id=owner_id
                            )
                        continue
                    try:
                        plaintext = read_model_field(record, name, key=old_key if version else None)
                    except EncryptionError as error:
                        report.failures.append(f"{spec.label}:{record.pk}:{name}: {error}")
                        continue
                    plaintexts[name] = plaintext
                    setattr(
                        record,
                        name,
                        encrypt_model_field(
                            record,
                            name,
                            plaintext,
                            key=new_key,
                            key_version=new_version,
                            user_id=owner_id,
                        ),
                    )
                    changed.append(name)

                index_columns: list[str] = []
                if changed and spec.indexes:
                    rebuilt = _rebuild_indexes(
                        record, spec, plaintexts=plaintexts, search_key=search_key
                    )
                    if rebuilt:
                        report.indexes_rebuilt += rebuilt
                        index_columns = [column for column, _, _ in spec.indexes]

                if changed:
                    record.save(update_fields=[*changed, *index_columns])
                    report.rows_rewritten += 1
                    report.fields_rewritten += len(changed)
    return report


def rotate_user(
    *,
    user: Any,
    old_key: bytes,
    new_key: bytes,
    new_version: int,
    search_key: bytes,
    batch_size: int = DEFAULT_BATCH_SIZE,
    models: Sequence[EncryptedModel] | None = None,
) -> RotationReport:
    """Move every encrypted value this user owns onto the new key."""

    report = RotationReport()
    for spec in models or ENCRYPTED_MODELS:
        report.merge(
            rotate_model(
                spec,
                user=user,
                old_key=old_key,
                new_key=new_key,
                new_version=new_version,
                search_key=search_key,
                batch_size=batch_size,
            )
        )
    return report


@dataclass
class VerificationReport:
    """Whether every value this user owns can still be read."""

    rows_checked: int = 0
    values_checked: int = 0
    unreadable: list[str] = field(default_factory=list)
    stale_versions: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.unreadable and not self.stale_versions

    def merge(self, other: VerificationReport) -> None:
        self.rows_checked += other.rows_checked
        self.values_checked += other.values_checked
        self.unreadable.extend(other.unreadable)
        self.stale_versions.extend(other.stale_versions)


def verify_user(
    *,
    user: Any,
    key: bytes,
    expected_version: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    models: Sequence[EncryptedModel] | None = None,
) -> VerificationReport:
    """Read every encrypted value under one key, before anything is retired.

    Two different failures, reported separately because they mean different
    things. **Unreadable** is data loss in progress: the value cannot be opened
    at all. **Stale** is a row rotation has not reached yet — still readable
    under the old key, so retiring that key now would create the first kind.
    """

    report = VerificationReport()
    for spec in models or ENCRYPTED_MODELS:
        owner_id = None if spec.owns_itself else user.pk
        queryset = spec.model().objects.filter(**{spec.owner_filter: user.pk})
        for batch in _batches(queryset, batch_size):
            for record in batch:
                report.rows_checked += 1
                for name in spec.fields:
                    stored = getattr(record, name) or ""
                    if not stored:
                        continue
                    report.values_checked += 1
                    version = envelope_key_version(stored)
                    if version != expected_version:
                        report.stale_versions.append(
                            f"{spec.label}:{record.pk}:{name}: version {version}"
                        )
                        continue
                    try:
                        read_model_field(record, name, key=key, user_id=owner_id)
                    except EncryptionError as error:
                        report.unreadable.append(f"{spec.label}:{record.pk}:{name}: {error}")
    return report
