# Architecture

Meelo is a single-user, self-hosted Django application. The web process serves
HTML and authenticated actions, a separate worker processes queued screenshots,
PostgreSQL stores the durable state, and temporary storage holds an upload only
for as long as the processing and retention policy require. The reverse proxy
is the only public-facing service.

## Runtime boundaries

```text
browser -> Caddy -> Django web -> PostgreSQL
                         |
                         +--> processing queue -> worker -> local OCR/parsers
                         |                         |
                         +<-- report/export <-------+
```

- Caddy terminates TLS, applies security headers, serves collected static
  assets, and proxies application requests. PostgreSQL is on an internal
  network and has no published host port.
- Django owns authentication, request authorization, review actions, ledger
  posting, categorization, reconciliation, reports, and export expiry.
- The worker claims `ProcessingJob` rows with bounded leases. It gets the
  document owner's data key through `get_worker_data_key`, never from a caller
  supplied user id, and clears the key scope after each job.
- OCR engines run locally. Their configuration and output are encrypted; the
  source image and temporary derivative are removed by the cleanup/retention
  path after processing or terminal failure.

## Three data layers

1. **Source documents** (`processing.SourceDocument`) keep upload metadata,
   SHA-256 fingerprints, lifecycle state, and encrypted filename/institution
   hints. They are evidence, not financial history.
2. **Imported observations** (`observations.ImportedObservation`) keep one
   parser candidate per screenshot row, confidence and risk projections, parser
   provenance, and reviewer decisions. Reprocessing creates a new OCR run and
   never rewrites a confirmed transaction.
3. **Canonical transactions** (`transactions.CanonicalTransaction`) are the
   reviewed events used by the ledger and reports. Idempotency keys and row
   locks make retries converge on one transaction.

Ledger entries post each canonical transaction into the user's chart of
accounts. Reconciliation matches observations and transactions without
combining their ownership or privacy boundaries. Reports read canonical
transactions, exclude voided/opening-balance rows as appropriate, and generate
short-lived exports that are deleted by a scheduled command.

## Application layout

| Concern | Code | Responsibility |
| --- | --- | --- |
| Shared security | `apps/core` | ownership filters, encryption, blind indexes, audit, key scope, metrics |
| Accounts and identity | `apps/users`, `apps/financial_accounts`, `apps/instruments` | user, keys, accounts, cards, and account-scoped forms |
| Upload and work | `apps/processing`, `apps/ocr` | validation, temporary storage, queue, OCR runs, cleanup |
| Parsing | `apps/parsing` | normalized tokens, dates, money, rows, institution registry, fixtures |
| Review | `apps/observations` | queue ranking, field corrections, acceptance, rejection, merge |
| Domain history | `apps/transactions`, `apps/ledger` | canonical lifecycle, double-entry posting, balances and invariants |
| Classification | `apps/categorization` | categories, aliases, rules, explainable classification |
| Reconciliation | `apps/reconciliation` | duplicates, transfers, refunds, matching, review explanations |
| Reporting | `apps/reports` | period totals, breakdowns, exports, expiry |
| HTTP wiring | `config/routes.py`, `config/urls.py`, app `views.py` | specification route table and Django endpoints |

Services contain domain transitions and audit calls; views authenticate the
request, resolve an owned object, validate a form, and delegate. Models hold
constraints and ownership validation. Templates render safe view-model values,
not raw encrypted columns. This separation is why a report cannot accidentally
sum an OCR observation or a view cannot read another user's row.

## Request and processing flow

An upload is validated for size, MIME type, dimensions, and image readability.
The source document is persisted, a processing job is queued, and the worker
claims it. The pipeline stores an encrypted OCR run and tokens, normalizes text,
selects an institution parser, and imports observations with parser version and
confidence metadata. A reviewer accepts, corrects, rejects, or merges each
candidate. Acceptance creates one canonical transaction, posts ledger entries,
and records an audit event without financial plaintext. Reports and exports then
operate only on canonical history.

Failure is explicit: error codes and retryability are stored on the job and
document, attempts are bounded, and cleanup runs on success and failure. A
partial OCR or parser result cannot become a canonical transaction because the
review transition is the only bridge between the layers.

## Security and ownership mapping

Value-bearing columns use AES-256-GCM through `EncryptedFieldsMixin`; exact
lookups use per-user HMAC blind indexes. Every owned query filters by `user_id`,
and model validation rejects cross-user foreign keys. Audit events are chained
and redact values. The master key is supplied by a deployment secret or
protected file, not by the database or source tree. See
[`docs/security.md`](security.md) and [`docs/database.md`](database.md) for the
security and schema details.

## Specification deviations

The implementation deliberately uses names that make runtime behavior clear:
`OcrRun.languages` is a list, `succeeded` is a predicate, and coordinates are
`left/top/right/bottom`. `User` keeps a numeric key because it is not publicly
referenced. The complete field-by-field comparison and reasons are in
[`DATAMODEL.md`](../DATAMODEL.md), while deployment procedures are in
[`RUNBOOK.md`](../RUNBOOK.md).
