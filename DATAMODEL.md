# Data Model Audit

Specification section 6 lists the fields and enumerations every core model must
carry. This is the record of where the implementation matches it, where it
differs, and why.

The table is not the source of truth — `apps/core/model_specification.py` is,
and `tests/test_model_specification.py` walks it on every run. The test fails in
both directions: a required field that disappears fails, a field recorded here
as deliberately absent that later appears fails, and a new column with no
justification fails. Prose drifts; a check does not.

```bash
uv run pytest tests/test_model_specification.py
```

## Result

Every model in section 6 exists. Ten of the twelve carry the specification's
field list exactly. Two differ, both in ways recorded below. Every enumeration
the specification writes out is present in a `TextChoices` class, and every one
of those classes is referenced by the column it constrains — an enumeration the
field does not use is documentation, not a constraint, so the audit checks for
that too.

## Renamed fields

The field is there under another name. Nothing is missing; the reader needs the
mapping.

| Model | Specification | Implemented | Why |
| --- | --- | --- | --- |
| `OcrRun` | `language` | `languages` | A run is given a list — Korean and English together — and one column holding `"ko+en"` would have to be parsed by everything that reads it. |
| `OcrRun` | `configuration_json_encrypted` | `configuration_encrypted` | The `_json` infix says how the value is serialized, which is not something a column name has to carry. |
| `OcrRun` | `success` | `succeeded` | Reads as a predicate at the call site: `if run.succeeded`. |
| `OcrToken` | `x1`, `y1`, `x2`, `y2` | `left`, `top`, `right`, `bottom` | `x1 > x2` is a bug you have to think about; `right < left` is one you can see. Check constraints enforce both orderings. |
| `CanonicalTransaction` | `created_by`, `reviewed_by` | `created_by_id`, `reviewed_by_id` | Django's own naming for a foreign key's column. |
| `ReconciliationMatch` | `reviewed_by` | `reviewed_by_id` | As above. |

## Absent fields

Two fields in section 6 are not implemented. Both are deliberate.

| Model | Field | Reasoning |
| --- | --- | --- |
| `OcrRun` | `error_message_encrypted` | A failed run stores `error_code` and nothing else. An engine's message tends to contain the temporary file path and fragments of the text it was reading when it gave up, and those are the two things this system is built not to keep. The code is also what the error catalogue renders and what the retry policy branches on, so the message would be stored for nobody. `SourceDocument` keeps its own encrypted message, which is the one a person actually sees. |
| `OcrToken` | `text_blind_index` | Nothing looks a token up by exact text — consensus matches tokens spatially and by normalized form, both in memory, within one document. Indexing every word of every screenshot would build a keyed, searchable word list of the entire corpus sitting next to the ciphertext it is meant to protect, and a per-token index is far more useful to an attacker holding the database than to any query this application makes. Merchant and counterparty text, which *is* searched, is blind-indexed on the observation and the transaction. |

Neither is needed for the MVP, so neither has follow-up work attached. If a
future feature needs one, deleting the entry from `absent` in
`apps/core/model_specification.py` is what makes the test demand the column.

## The `User` primary key

Section 6 opens with "use UUID primary keys for externally referenced records",
and every model here does — except `User`, which uses Django's `BigAutoField`.

A user is never externally referenced: there is no `/users/<id>/` route, no user
identifier in an export, and public registration is disabled. The identifier
appears in exactly one place a person could see, the admin site, which is behind
two-factor authentication. Meanwhile it is the most-joined column in the schema —
every owned table filters on it — and it is inside the associated data of every
encrypted field and every blind index, where a 36-character string costs
something on every read.

The trade is enumerable identifiers for a household of one person against a
narrower index on every table. For a system whose threat model is a stolen disk
rather than a hostile signup, the narrower index is worth more.

## Additive fields

Everything the implementation carries beyond section 6, with the section that
requires it. The justifications live next to the field lists in
`apps/core/model_specification.py` so they cannot drift from the code, and the
test refuses any column that has none.

| Model | Fields | Purpose |
| --- | --- | --- |
| `SourceDocument` | `cleanup_error_code`, `temporary_path`, `perceptual_hash`, `retention_policy`, `retention_deadline` | Temporary-file lifecycle (section 9) and near-identical screenshot detection (16.2). A failed cleanup is kept apart from a failed parse so a document that processed correctly is not reported as broken. |
| `OcrRun` | `user_id`, `model_versions`, `preprocessing_encrypted`, `selected_preprocessing_variant`, `error_code`, `duration_ms`, `created_at` | Reproducing a result after a model upgrade (section 11), and the CPU and wall-clock bounds the same section sets. |
| `OcrToken` | `user_id`, `normalized_text_encrypted`, `page_number`, `block_number`, `paragraph_number`, `sequence` | Consensus matching (13.1–13.2). Tesseract's own layout numbering is carried through unchanged so a token can be traced back to the engine's view of the page. |
| `ImportedObservation` | `ocr_run_id`, `row_index`, parser provenance, `review_flags` and its five queryable projections, `risk_score`, `reviewed_by_id`, `reviewed_at`, `corrected_fields`, `merged_into_id` | Review ranking (13.4, section 19) and parser accuracy measurement (31.3). The boolean projections exist because the queue has to filter and sort in the database — JSON containment is not portable across SQLite and PostgreSQL, and ranking in Python would only order rows within a page. |
| `CanonicalTransaction` | `category_source`, `source_idempotency_key` | Categorization provenance (section 18), so re-classification cannot overwrite a correction a person made; and convergence, so a retried worker produces one transaction rather than two. |
| `ReconciliationMatch` | `reviewed_at` | When the decision was made, beside who made it. |

`user_id` on `OcrRun` and `OcrToken` is denormalized from the parent document.
Section 21.3 requires every query for owned data to filter on the owner, and a
join to reach the owner is a join a query can be written without.

## Models outside section 6

Section 6 tabulates the financial domain. These are required by later sections
that do not restate their fields, and the audit lists them so it covers the whole
schema rather than the part that happens to be tabulated.

| Model | Section |
| --- | --- |
| `users.UserDataKey` | 22.2, per-user wrapped data keys and their versions |
| `core.AuditEvent` | 23, the hash-chained audit log |
| `processing.ProcessingJob` | 3.3, the database-backed work queue |
| `ledger.ChartOfAccounts`, `ledger.LedgerAccount` | 7, the double-entry chart the ledger posts into |
| `categorization.CategoryRule` | 18, user categorization rules |
| `reconciliation.NearDuplicateDocument` | 16.2, perceptually similar screenshots — kept apart from transaction matches, because two images looking alike is a statement about pixels and must never be presented as though money had been matched |
| `reports.TransactionExport` | 25.5, generated exports and their expiry |

## Enumerations

Every value section 6 writes out is present. One gap was closed by this audit:
`OcrRun.engine` was a free-text column, so a typo could have created a third
engine that the consensus model would have counted as an independent opinion.
It now uses an `Engine` choices class holding `paddleocr` and `tesseract`.

Three enumerations carry values beyond the specification's list, which the audit
allows — a choices class may hold more, never fewer:

- `PaymentInstrument.InstrumentType` adds `prepaid_card` and `other`. Section 6.3
  describes the field as "a debit card, credit card, virtual card, or similar"
  rather than enumerating it.
- `ImportedObservation.Direction` and `Category.CategoryType` are not enumerated
  in section 6 at all; their values come from 15.3 and section 18.
