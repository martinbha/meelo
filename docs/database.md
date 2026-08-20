# Database model and query guide

This page is the contributor-facing map of the Django schema. Deployment roles,
networking, backups, and PostgreSQL ownership controls are documented in
[DATABASE.md](../DATABASE.md).

## Ownership and boundaries

Every financial row belongs to one `users.User` through a `user` foreign key,
directly or through its parent. Views and services must start from
`apps.core.ownership.owned_queryset` (or `get_owned_object_or_404`) and never
accept an owner id from a request. Model validation covers cross-table
ownership that a database constraint cannot express. `LedgerEntry` belongs to
the transaction's user; `LedgerAccount` belongs to the chart's user.

The schema is split into three data layers:

1. `processing.SourceDocument` and `ocr.OcrRun`/`OcrToken` retain the uploaded
   source and reproducible OCR run.
2. `observations.ImportedObservation` holds parser candidates and review state.
3. `transactions.CanonicalTransaction` is the only source for ledger and report
   totals. An observation reaches it only through an explicit review action.

## Model inventory

| App | Models | Role |
| --- | --- | --- |
| `users` | `User`, `UserDataKey`, `UserSearchKey` | Account identity and wrapped per-user keys |
| `core` | `AuditEvent`, `RotationCheckpoint` | Hash-chained audit trail and resumable rotation |
| `processing` | `SourceDocument`, `ProcessingJob` | Upload metadata, retention, and queue state |
| `ocr` | `OcrRun`, `OcrToken` | Engine configuration, output, and token coordinates |
| `parsing` | no database models | Parser contracts and institution registry |
| `observations` | `ImportedObservation` | OCR/parser candidates and reviewer decisions |
| `financial_accounts` | `FinancialAccount` | Owned bank, cash, credit, loan, and investment accounts |
| `instruments` | `PaymentInstrument` | Cards and other payment instruments |
| `categorization` | `Category`, `MerchantAlias`, `CategoryRule` | User taxonomy and classification rules |
| `transactions` | `CanonicalTransaction` | Confirmed financial events and idempotency |
| `ledger` | `ChartOfAccounts`, `LedgerAccount`, `LedgerEntry` | Double-entry postings |
| `reconciliation` | `ReconciliationMatch`, `NearDuplicateDocument` | Match decisions and perceptual duplicate candidates |
| `reports` | `TransactionExport` | Temporary, expiring report exports |

All externally referenced domain records use UUID primary keys. `User` keeps a
numeric primary key because it has no public identifier and is the most-joined
column; user ids are still included in ciphertext associated data and blind
indexes.

## Encrypted and indexed fields

Columns ending in `_encrypted` are declared on the model's
`EncryptedFieldsMixin`. Values are AES-256-GCM envelopes, not serialized
plaintext. The mixin binds each value to model, row, field, owner, and key
version. Empty optional values remain empty so absence is distinguishable from
an unreadable value.

| Model | Encrypted fields | Queryable companion |
| --- | --- | --- |
| `FinancialAccount` | name, institution, masked identifier, opening balance | name/institution/identifier blind indexes |
| `PaymentInstrument` | name, issuer | name blind index |
| `Category` | name | name blind index |
| `MerchantAlias` | alias, normalized merchant | two merchant blind indexes |
| `CategoryRule` | merchant pattern, min/max amount | merchant-pattern blind index |
| `ImportedObservation` | merchant, normalized merchant, counterparty, amount, balance, approval code, source region | merchant and approval-code blind indexes |
| `CanonicalTransaction` | merchant, counterparty, amount, notes | merchant and counterparty blind indexes |
| `ChartOfAccounts` / `LedgerAccount` | account name | name blind index |
| `LedgerEntry` | amount | none; reached through its transaction |
| `OcrRun` / `OcrToken` | configuration, preprocessing, raw output, token text | none |
| `ReconciliationMatch` | match features JSON | none |
| `SourceDocument` | filename, institution guess, error message | file hash is an exact duplicate fingerprint |

Blind indexes are HMAC-SHA-256 values scoped to the owning user and search-key
version. They permit exact lookup only; they cannot sort, range-query, or
recover the source text. Financial amounts remain encrypted rather than hashed
because reports must decrypt them to add and display them. The database
plaintext audit command and the model declaration test guard this boundary.

## Constraints and migrations

Use Django migrations for every schema change. Run
`python manage.py makemigrations --check --dry-run` before committing. Model
`TextChoices` and database `CHECK` constraints reject invalid statuses,
directions, currencies, dates, confidence values, and ownership relationships.
Unique constraints provide idempotency for imported rows and source-generated
transactions; application row locks handle the race before the constraint is
checked.

Do not query an encrypted field directly. Use its blind index for exact lookup,
load the owner-scoped data key, and decrypt through `read_field` or the domain
service. Never log a decrypted value, include it in an audit event, or copy it
to a cache. A schema change that adds a value-bearing field must add it to
`encrypted_fields`, rotation metadata, the model specification, and tests in
the same change.
