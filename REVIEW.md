# Review and Reconciliation Guide

How a parsed screenshot row becomes confirmed financial history, and what stops
it from becoming history by accident.

## The two-stage model

The system never turns OCR output into a transaction on its own.

```text
screenshot → OCR runs → parser rows → ImportedObservation → (a person decides) → CanonicalTransaction → LedgerEntry
```

`ImportedObservation` is what a parser *thought* it saw. `CanonicalTransaction`
is what the user *confirmed*. Reports and the ledger read only the second.

That separation is what makes reprocessing safe: a new OCR pass creates new
observations beside the old ones and cannot disturb a transaction that has
already been accepted.

## Import (`apps.observations.services`)

`import_parser_selection` converts one parse into stored rows. Two properties
matter:

- **Atomic** — either every row of a screenshot lands or none does, so review
  never sees half a page.
- **Idempotent** — a unique constraint on `(ocr_run, parser_name,
  parser_version, row_index)` means a retried worker cannot create a second
  copy. A *new parser version* imports alongside the old rows rather than
  replacing them, so an improved parser can be compared against its predecessor.

Values are encrypted per user (merchant, amount, balance, approval code, source
region). What stays in clear is what has to be queried or sorted: dates,
currency, direction, confidences, and the review flags.

### Confidence is stored twice on purpose

`ocr_confidence` and `parser_confidence` are kept apart because they fail
differently — a blurry photograph lowers the first, a layout the parser does not
understand lowers the second. `overall_confidence` is the weaker of the two, so
a sharp image parsed badly never looks trustworthy.

## The review queue (`apps.observations.queue`)

Every observation stays in the queue until it is accepted, corrected, rejected,
or merged. Nothing leaves silently.

Ordering is by **risk**, and the ordering happens in the database. `risk_score`
is stored on the row (see `apps.observations.risk`) because a high-risk row must
sort ahead of routine work across the *whole* queue, not merely within the page
it happens to land on.

The score is the single worst problem a row has, never a sum: three cosmetic
problems must not outrank one ambiguous amount.

| Signal | Risk |
| --- | --- |
| Ambiguous amount | 100 |
| Missing amount | 95 |
| Balance chain broken | 90 |
| Parser error on the row | 85 |
| Unknown or missing direction | 80 |
| Unmapped account or card | 75 |
| Missing or ambiguous date | 70 |

`review_flags` keeps the human-readable list for display; the boolean columns
(`amount_uncertain`, `balance_mismatched`, `has_missing_fields`,
`is_settlement_candidate`) exist so the queue can filter in SQL — JSON
containment is not portable across the databases this project runs on.

**When you change a row, re-score it.** `rescore_observation` recomputes the
stored score and projections. Mapping an account or correcting a flagged field
without calling it leaves the queue ranking a row as though it were still
blocked.

### Reconciliation candidates

Duplicate, transfer, and settlement filters come from the reconciliation layer.
The queue takes them as `match_ids` rather than importing that app, which keeps
the dependency pointing one way: reconciliation knows about observations, not
the reverse. The view layer is the only place the two meet.

## Reviewer actions (`apps.observations.review`)

| Action | Effect |
| --- | --- |
| `correct_observation` | Applies field corrections, records which fields changed, clears the flags they answer |
| `accept_observation` | Creates the canonical transaction, optionally posts the ledger |
| `reject_observation` | Discards the candidate; it is stored but never reported |
| `merge_observations` | Folds duplicates into one row, both sources preserved |
| `request_reprocess` | Queues another OCR pass, preserving prior runs and observations |

Reprocessing goes through `transition_document` and enqueues a `ProcessingJob`.
A document moved to `queued` without a job would wait forever, so both steps
belong together.

Rules worth knowing before you extend any of them:

- **Currency corrections re-encode the amount.** The encrypted amount carries
  its own currency suffix; leaving it stale would post in the currency the
  reviewer just corrected away from. Corrections are applied in a fixed order so
  a currency and an amount submitted together agree.
- **An amount cannot be corrected without a currency.** A row whose amount never
  parsed has no currency either; guessing one would post a real amount in the
  wrong currency. Submit both in the same correction.
- **Risky rows demand explicit confirmation.** A row scoring at or above 80, or
  with a disputed amount, refuses acceptance unless `confirmed=True`.
- **A card must belong to the account it posts against.** Acceptance enforces the
  same instrument/account compatibility as the manual creation path.
- **Merges never chain.** A row already merged into another cannot become the
  winner of a second merge, so every merged row points at a surviving one.
- **Acceptance is idempotent.** A second call returns the transaction already
  created rather than making another.
- **Ledger posting is atomic with the status change.** If the posting fails, the
  acceptance rolls back with it — a row can never end up accepted without its
  entries, or posted twice.
- **Audit metadata carries names, never values.** Corrections log field names; a
  rejection logs the reason's *length*. The audit log must not become a second,
  unencrypted copy of the financial data it describes.

## Reconciliation (`apps.reconciliation`)

Reconciliation resolves several views of one real event. It **proposes**;
it never decides.

### Duplicates

Two mechanisms, deliberately separate:

- A **deterministic key** — an identical approval code, or the tuple
  `(instrument, date, amount, currency, direction)` — finds the certain cases.
  A pair reached this way is always surfaced, whatever it scores.
- A **score** (specification 16.3) ranks the uncertain ones:

```text
Exact amount             +30    Same approval code    +30
Same mapped account/card +25    Same balance after    +15
Date within one day      +15    Same source type       +5
Same direction           +10    Merchant similarity   +10

>= 90  propose merge      65-89  review candidate      < 65  keep separate
```

Same user is a hard requirement, not a weighted feature.

`AUTOMATIC_MERGE_ENABLED` is `False` and should stay that way for the initial
release: even a perfect score only produces a candidate.

### Near-identical screenshots

A difference hash (`dhash8`) catches screenshots differing only by crop or
recompression. It is optional — `NEAR_DUPLICATE_DETECTION_ENABLED=false` turns
it off and exact SHA-256 duplicate detection is unaffected, because nothing else
depends on the perceptual hash.

Near-duplicate links live in their own model, apart from `ReconciliationMatch`:
two images looking alike is a statement about pixels, not about money, and must
never be presented as though a transaction had been matched.

### Transaction matching

`apps.reconciliation.matching` proposes debit-card/bank pairs, credit-card
settlements, internal transfers, and refunds. A proposal scoring below 85
`needs_review` and is never linked automatically.

The rule that matters most: **a card settlement is not spending.** The money it
moves was already counted when the purchases were made, so classifying a
withdrawal to a card issuer as an expense would double-count every card purchase
that month. `summarize_settlement` reconciles multiple payments, partial
payments, and refunds against one statement balance — and keeps fees and
interest as separate reportable expenses, because those genuinely are spending.

Callers pair card-source rows against bank-source rows. The matcher does not
know which screenshot a row came from, so feeding it two rows from the same card
statement will happily pair two similar purchases.

## Serving the original screenshot

`DocumentImageView` streams the stored original after an ownership check, with
`Cache-Control: private, no-store`. Screenshots are never served from a public
media directory — a bank statement image must not be reachable by anyone who
guesses a URL.

## Testing

```bash
uv run pytest tests/test_observation_import.py tests/test_review_queue.py
uv run pytest tests/test_review_actions.py tests/test_review_views.py
uv run pytest tests/test_duplicate_detection.py tests/test_reconciliation_matching.py
```

Scoring and matching are pure functions over `ObservationFacts`, so they are
tested without a database or a data key. Decryption happens once in the caller.
