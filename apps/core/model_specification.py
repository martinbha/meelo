"""Specification section 6 written down as something a test can check.

The specification lists the fields and enumerations every core model is
required to carry. Code drifts from prose quietly: a field gets renamed during
a refactor, a choice value is dropped because nothing referenced it that week,
and the document that said otherwise is still sitting in ``docs/`` being wrong.

So the field lists live here as data instead. :mod:`tests.test_model_specification`
walks them and fails when a required field or choice value disappears, which
turns "the model matches the specification" from a claim into a check.

Deviations are part of the contract rather than exceptions to it. Every field
the implementation renamed, added, or deliberately left out is recorded with a
justification, and the test asserts the record is still true in both
directions — a documented-as-missing field that later appears fails just as
loudly as a required field that vanishes. A deviation table nobody verifies
becomes fiction within a release; this one cannot.

The prose version, with the reasoning behind each deviation, is ``DATAMODEL.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ModelSpecification:
    """One model's required shape, plus every recorded difference from it."""

    #: Specification subsection, e.g. ``"6.2"``. Quoted in failure messages so a
    #: reader can go straight to the paragraph that disagrees with the code.
    section: str
    #: Django ``app_label.ModelName``, resolved lazily so this module imports
    #: without the app registry being ready.
    label: str
    #: Field names exactly as the specification writes them.
    fields: tuple[str, ...]
    #: Specification name -> implemented attribute name, for fields that exist
    #: under a different name.
    renamed: Mapping[str, str] = field(default_factory=dict)
    #: Specification name -> why the implementation does not have it.
    absent: Mapping[str, str] = field(default_factory=dict)
    #: Implemented attribute name -> what it is for. Everything the model holds
    #: beyond the specification list has to appear here.
    additional: Mapping[str, str] = field(default_factory=dict)
    #: ``TextChoices`` attribute on the model class -> the values the
    #: specification enumerates. The class may hold more; it may not hold fewer.
    enumerations: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def implemented_name(self, specification_field: str) -> str:
        return self.renamed.get(specification_field, specification_field)

    def expected_attributes(self) -> set[str]:
        """Every attribute name this model should carry, renames applied."""

        return {
            self.implemented_name(name) for name in self.fields if name not in self.absent
        } | set(self.additional)


#: Ownership columns the specification implies rather than lists. A record that
#: belongs to a person needs to say which person; the specification says so in
#: section 21.3 and does not repeat it in every field list.
_OWNER = "Owner scope, so ownership filtering does not need a join (specification 21.3)."

CORE_MODELS: tuple[ModelSpecification, ...] = (
    ModelSpecification(
        section="6.1",
        label="users.User",
        fields=(
            "id",
            "email",
            "password",
            "is_active",
            "is_staff",
            "is_superuser",
            "date_joined",
            "last_login",
            "encryption_key_version",
            "created_at",
            "updated_at",
        ),
        additional={},
    ),
    ModelSpecification(
        section="6.2",
        label="financial_accounts.FinancialAccount",
        fields=(
            "id",
            "user_id",
            "name_encrypted",
            "name_blind_index",
            "institution_encrypted",
            "institution_blind_index",
            "account_type",
            "masked_identifier_encrypted",
            "identifier_last_four",
            "currency",
            "opening_balance_encrypted",
            "is_active",
            "created_at",
            "updated_at",
        ),
        additional={
            "identifier_blind_index": "Keyed token for the account number's digits, so a parser "
            "that reads a masked identifier off a screenshot can find the account without "
            "decrypting every account the user has (specification 22.4).",
        },
        enumerations={
            "AccountType": (
                "checking",
                "savings",
                "cash",
                "credit_card_liability",
                "loan",
                "investment",
                "other_asset",
                "other_liability",
            )
        },
    ),
    ModelSpecification(
        section="6.3",
        label="instruments.PaymentInstrument",
        fields=(
            "id",
            "user_id",
            "name_encrypted",
            "name_blind_index",
            "instrument_type",
            "last_four",
            "financial_account_id",
            "settlement_account_id",
            "issuer_encrypted",
            "is_active",
            "created_at",
            "updated_at",
        ),
    ),
    ModelSpecification(
        section="6.4",
        label="processing.SourceDocument",
        fields=(
            "id",
            "user_id",
            "file_sha256",
            "original_filename_encrypted",
            "mime_type",
            "file_size",
            "image_width",
            "image_height",
            "source_institution_guess_encrypted",
            "source_type",
            "processing_status",
            "error_code",
            "error_message_encrypted",
            "uploaded_at",
            "processing_started_at",
            "processing_completed_at",
            "processing_attempt_count",
            "next_processing_attempt_at",
            "original_deleted_at",
        ),
        additional={
            "cleanup_error_code": "Why deleting the temporary file failed, kept apart from "
            "the processing error so a successful parse is not reported as a failure.",
            "temporary_path": "Where the original is while it is being processed "
            "(specification 9). Cleared once the file is gone.",
            "perceptual_hash": "Difference hash for near-identical screenshot detection "
            "(specification 16.2).",
            "retention_policy": "How long the original may be kept (specification 9).",
            "retention_deadline": "When the retention policy expires, so the cleanup command "
            "can select rows in the database rather than in Python.",
            "source_type_override": "What a reviewer said the screenshot actually is "
            "(specification 14, 19). Kept beside the detected guess rather than overwriting "
            "it, so detection accuracy stays measurable and the override can be cleared.",
            "institution_override": "The institution parser a reviewer chose, by name. "
            "See source_type_override.",
        },
        enumerations={
            "SourceType": (
                "bank_transaction_list",
                "bank_transaction_detail",
                "bank_transfer_confirmation",
                "card_transaction_list",
                "card_transaction_detail",
                "credit_card_statement",
                "credit_card_payment",
                "unknown",
            ),
            "Status": (
                "pending",
                "validating",
                "queued",
                "preprocessing",
                "ocr_running",
                "parsing",
                "ready_for_review",
                "confirmed",
                "failed",
                "deleted",
            ),
        },
    ),
    ModelSpecification(
        section="6.5",
        label="ocr.OcrRun",
        fields=(
            "id",
            "source_document_id",
            "engine",
            "engine_version",
            "language",
            "configuration_json_encrypted",
            "raw_output_encrypted",
            "started_at",
            "completed_at",
            "success",
            "error_message_encrypted",
        ),
        renamed={
            "language": "languages",
            "configuration_json_encrypted": "configuration_encrypted",
            "success": "succeeded",
        },
        absent={
            "error_message_encrypted": "A failed run records error_code only. Engine "
            "messages carry file paths and fragments of the image's text, and the code is "
            "what the error catalogue and the retry policy actually read.",
        },
        additional={
            "user_id": _OWNER,
            "model_versions": "Which OCR model weights produced this run, so a result can be "
            "reproduced after an upgrade (specification 11).",
            "preprocessing_encrypted": "The preprocessing chain applied before this run.",
            "selected_preprocessing_variant": "Which variant won, kept readable so variant "
            "selection can be measured without decrypting.",
            "error_code": "Catalogue code for a failed run (specification 27).",
            "duration_ms": "Wall-clock cost, for the CPU and time bounds in specification 11.",
            "created_at": "Row creation time, distinct from the engine's own start time.",
        },
        enumerations={"Engine": ("paddleocr", "tesseract")},
    ),
    ModelSpecification(
        section="6.6",
        label="ocr.OcrToken",
        fields=(
            "id",
            "ocr_run_id",
            "text_encrypted",
            "text_blind_index",
            "confidence",
            "x1",
            "y1",
            "x2",
            "y2",
            "line_number",
            "word_number",
            "created_at",
        ),
        renamed={"x1": "left", "y1": "top", "x2": "right", "y2": "bottom"},
        absent={
            "text_blind_index": "Nothing looks a token up by its exact text. Indexing every "
            "word of every screenshot would publish a keyed word list of the whole corpus, "
            "which is a far better target than the ciphertext it sits beside.",
        },
        additional={
            "user_id": _OWNER,
            "normalized_text_encrypted": "The normalized form used for consensus matching "
            "(specification 13.1), stored so matching does not re-normalize on every read.",
            "page_number": "Tesseract layout coordinates, carried through unchanged so a "
            "token can be traced back to the engine's own view of the page.",
            "block_number": "See page_number.",
            "paragraph_number": "See page_number.",
            "sequence": "Reading order within the run, which is what the unique constraint "
            "and the review display order are built on.",
        },
    ),
    ModelSpecification(
        section="6.7",
        label="observations.ImportedObservation",
        fields=(
            "id",
            "source_document_id",
            "user_id",
            "financial_account_guess_id",
            "payment_instrument_guess_id",
            "occurred_at",
            "posted_at",
            "merchant_raw_encrypted",
            "merchant_normalized_encrypted",
            "merchant_blind_index",
            "counterparty_raw_encrypted",
            "amount_encrypted",
            "currency",
            "direction",
            "balance_after_encrypted",
            "approval_code_encrypted",
            "installment_months",
            "transaction_type_guess",
            "category_guess_id",
            "ocr_confidence",
            "parser_confidence",
            "overall_confidence",
            "source_region_json_encrypted",
            "review_status",
            "canonical_transaction_id",
            "created_at",
            "updated_at",
        ),
        additional={
            "import_key": "Deterministic document, OCR run, parser, and row identity backed by "
            "a database uniqueness constraint, so duplicate imports fail closed.",
            "ocr_run_id": "Which run produced this candidate, so a reprocessed document can "
            "show old and new side by side.",
            "row_index": "Position on the screenshot, so review reads top to bottom.",
            "parser_name": "Parser provenance, so a fixed parser can be told from a fixed "
            "screenshot (specification 14).",
            "parser_version": "See parser_name.",
            "parser_output_version": "See parser_name.",
            "review_flags": "Names of the problems the parser raised. Names only, never values.",
            "requires_review": "Whether the review rules in specification 13.4 force a "
            "reviewer decision.",
            "amount_uncertain": "Queryable projection of review_flags, because the queue has "
            "to filter and rank in the database rather than a page at a time.",
            "balance_mismatched": "See amount_uncertain.",
            "has_missing_fields": "See amount_uncertain.",
            "is_settlement_candidate": "See amount_uncertain.",
            "risk_score": "Worst-problem score the queue sorts on.",
            "reviewed_by_id": "Who decided, matching the audit requirement in section 23.",
            "reviewed_at": "When they decided.",
            "approval_code_blind_index": "Keyed token for the approval code, which identifies one "
            "authorisation exactly and is the strongest duplicate signal there is "
            "(specification 16.3, 22.4).",
            "corrected_fields": "Which fields a reviewer changed, for the accuracy metrics in "
            "specification 31.3.",
            "merged_into_id": "The surviving row when this one was merged, so the merge can "
            "be explained and undone.",
        },
        enumerations={
            "ReviewStatus": ("unreviewed", "accepted", "corrected", "rejected", "merged")
        },
    ),
    ModelSpecification(
        section="6.8",
        label="transactions.CanonicalTransaction",
        fields=(
            "id",
            "user_id",
            "transaction_type",
            "occurred_at",
            "posted_at",
            "merchant_encrypted",
            "merchant_blind_index",
            "counterparty_encrypted",
            "counterparty_blind_index",
            "amount_encrypted",
            "currency",
            "category_id",
            "financial_account_id",
            "payment_instrument_id",
            "status",
            "notes_encrypted",
            "created_by",
            "reviewed_by",
            "created_at",
            "updated_at",
        ),
        renamed={"created_by": "created_by_id", "reviewed_by": "reviewed_by_id"},
        additional={
            "category_source": "What decided the category, so re-classification cannot "
            "overwrite a correction a person made (specification 18).",
            "source_idempotency_key": "Deterministic key of whatever produced this row, so a "
            "retried worker converges on one transaction instead of two.",
        },
        enumerations={
            "TransactionType": (
                "purchase",
                "income",
                "bank_transfer",
                "internal_transfer",
                "credit_card_payment",
                "cash_withdrawal",
                "refund",
                "fee",
                "interest",
                "loan_payment",
                "adjustment",
                "unknown",
            ),
            "Status": ("draft", "confirmed", "voided"),
        },
    ),
    ModelSpecification(
        section="6.9",
        label="ledger.LedgerEntry",
        fields=(
            "id",
            "transaction_id",
            "account_id",
            "entry_type",
            "amount_encrypted",
            "currency",
            "created_at",
        ),
        enumerations={"EntryType": ("debit", "credit")},
    ),
    ModelSpecification(
        section="6.10",
        label="categorization.Category",
        fields=(
            "id",
            "user_id",
            "name_encrypted",
            "name_blind_index",
            "parent_id",
            "category_type",
            "is_system",
            "created_at",
            "updated_at",
        ),
    ),
    ModelSpecification(
        section="6.11",
        label="categorization.MerchantAlias",
        fields=(
            "id",
            "user_id",
            "alias_encrypted",
            "alias_blind_index",
            "normalized_merchant_encrypted",
            "normalized_merchant_blind_index",
            "default_category_id",
            "payment_instrument_id",
            "created_at",
            "updated_at",
        ),
    ),
    ModelSpecification(
        section="6.12",
        label="reconciliation.ReconciliationMatch",
        fields=(
            "id",
            "user_id",
            "left_observation_id",
            "right_observation_id",
            "match_type",
            "match_score",
            "match_features_json_encrypted",
            "status",
            "reviewed_by",
            "created_at",
            "updated_at",
        ),
        renamed={"reviewed_by": "reviewed_by_id"},
        additional={"reviewed_at": "When the decision was made, alongside who made it."},
        enumerations={
            "MatchType": (
                "duplicate_observation",
                "debit_card_bank_match",
                "credit_card_payment",
                "internal_transfer",
                "refund_match",
                "statement_membership",
            )
        },
    ),
)

#: Models the implementation adds beyond section 6, each required by a later
#: section that the data-model chapter does not restate. Listed so the audit
#: covers the whole schema rather than only the part the specification tabulates.
SUPPORTING_MODELS: Mapping[str, str] = {
    "users.UserDataKey": "Per-user wrapped data keys and their versions (specification 22.2).",
    "users.UserSearchKey": "Per-user wrapped blind-index keys, versioned separately from the "
    "data keys so the two can rotate on their own schedules (specification 22.4).",
    "core.AuditEvent": "The hash-chained audit log (specification 23).",
    "core.RotationCheckpoint": "How far a key rotation got, so a resumed run does not re-read "
    "the whole history to find where it stopped (specification 22.6).",
    "core.WorkerHeartbeat": "Latest worker liveness timestamps for operational health checks.",
    "processing.ProcessingJob": "The database-backed work queue (specification 3.3).",
    "ledger.ChartOfAccounts": "The double-entry chart the ledger posts into (specification 7).",
    "ledger.LedgerAccount": "Ledger accounts and their normal balances (specification 7).",
    "categorization.CategoryRule": "User categorization rules (specification 18).",
    "reconciliation.NearDuplicateDocument": "Perceptually similar screenshots, kept apart from "
    "transaction matches (specification 16.2).",
    "reports.TransactionExport": "Generated exports and their expiry (specification 25.5).",
    "reports.QualityMetricDaily": "Daily privacy-safe parser quality aggregates "
    "(specification 31.3).",
}
