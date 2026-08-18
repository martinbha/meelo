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
  created rather than making another. Two mechanisms hold this, on purpose:
  `select_for_update` makes the common case cheap, and a unique
  `source_idempotency_key` per user makes the uncommon case correct. A lock only
  helps while both attempts are in the same database holding the same lock and
  neither has crashed; the constraint holds even when that stops being true.
  Every origin has a key — `observation:<id>`, `transfer:<match id>`,
  `refund:<match id>` — and `save_once` turns a losing insert into the winner's
  transaction rather than an error. Manual entry has no key, because two
  identical manual entries are a legitimate thing for a person to make.
- **Ledger posting is atomic with the status change.** If the posting fails, the
  acceptance rolls back with it — a row can never end up accepted without its
  entries, or posted twice.
- **Audit metadata carries names, never values.** Corrections log field names; a
  rejection logs the reason's *length*. The audit log must not become a second,
  unencrypted copy of the financial data it describes.

## The transaction lifecycle (`apps.transactions.lifecycle`)

A canonical transaction is a proposal, then history, then possibly withdrawn
history.

| From | May become |
| --- | --- |
| `draft` | `confirmed`, `voided` |
| `confirmed` | `voided` |
| `voided` | nothing |

The table is the whole rule. Anything not in it raises `TransitionError` and
changes nothing — including a status string that is not a status, which must not
be read as an unlisted transition and let through. It is a table rather than an
`if` in whichever service happens to be changing the status, because the second
such `if` is where the two disagree.

**Voiding is not deletion.** The row stays, reports stop counting it
(`REPORTABLE_STATUSES` is draft and confirmed), and it audits as
`transaction_voided`. `transaction_deleted` is reserved for the removal path
that also reverses the ledger.

**Confirmation is the line for editing, not posting.** `update_manual_transaction`
refuses anything that is not a draft. The earlier rule refused edits only once
ledger entries existed, which left a window: a confirmed transaction that reports
already counted could be rewritten with nothing recording that it had ever said
something else.

A confirmed transaction is still correctable — people misread receipts — but
only through `correct_confirmed_transaction`, which requires a reason and
records which fields changed. The point is not that corrections are rare; it is
that a correction leaves a trace and an ordinary edit does not, so the two must
not share a code path. Only names reach the audit record, never values
(specification 23).

Corrections stop at the ledger. Amount, currency, type, account, and instrument
are what the postings were built from, so correcting one of those on a posted
transaction is refused: the fix is a reversal and a re-entry, not an edit that
leaves entries describing a transaction that no longer exists.

## Deleting a transaction (`apps.transactions.deletion`)

"Delete" is the word a person uses; it is not what happens. The row stays, its
ledger entries stay, and an opposing entry is written for each of them. What
changes is that the transaction becomes `voided`, so reports stop counting it,
and every observation that fed it goes back into the review queue as
`unreviewed`.

Deleting the rows would be the obvious implementation and the wrong one. A set
of books that can be edited backwards cannot explain itself: the money would be
missing from the totals with nothing recording that it had ever been there.

Three things happen together or not at all.

| Step | Why it cannot be skipped |
| --- | --- |
| Reverse the postings | A void alone leaves reports and ledger disagreeing about whether the money moved |
| Void the transaction | A reversal alone leaves a transaction reports still count against a ledger that says it never happened |
| Release the observations | Otherwise they are stranded: accepted, pointing at a transaction nobody can see, and out of the queue |

`confirmed=True` is required, and the page in front of it says what will happen
rather than asking "are you sure". The consequences are not obvious from the
button, and repeating the click does not undo them — the reversal is already
written.

Reversing twice is refused. A second pass leaves the accounts balanced and the
entry count doubled, and every later reader would have to know that half the
rows are noise. Detection is per account rather than per transaction: a
balanced posting and a reversed one both net to zero overall, but only a
reversed one leaves every individual account at zero.

## Reconciliation (`apps.reconciliation`)

Reconciliation resolves several views of one real event. It **proposes**;
it never decides.

### The candidate queue

Every proposal arrives with its reasons attached. A score alone would leave a
reviewer nothing to check but the number, and deferring to the number is the one
thing this queue exists to prevent. `match_features_json_encrypted` stores the
feature *names* that produced the score — never values, so a match row cannot
become a second unencrypted copy of the data it describes — and
`apps.reconciliation.explanations` turns them into sentences.

`tests/test_reconciliation_queue.py` reads the feature literals out of the
scoring modules with `ast` and fails if any lacks a sentence, so a new signal
cannot quietly reach a reviewer as a bare identifier.

The queue also names what confirming would produce: an internal transfer becomes
`internal_transfer`, a refund match becomes `refund`, and so on. A duplicate
names nothing, because merging two views of one event does not decide what the
event was.

`link_observations` records a relationship the matcher missed — a refund whose
merchant OCR'd badly, a transfer one app dated a week late. It scores 100 with a
single `manual_link` reason, so the queue says the evidence is the user's own
judgement rather than implying the matcher noticed something. Linking creates a
candidate; confirming it is still a separate step through the same workflow as
any other.

A manual link is also the **only** thing that reopens a rejected pairing.
`record_match` leaves decided candidates alone, so re-running detection can never
resurrect one — but the person who dismissed it may change their mind.

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

### Refunds

A refund is not income. Someone who bought a coat for 200,000 and returned it is
back where they started, not 200,000 better off, and the coat's category should
end up showing nothing. Counting the credit as income would inflate income and
spending at once and leave the category wrong.

`apps.reconciliation.refunds.confirm_refund_match` therefore always produces a
`refund` transaction — never `income` — carrying **the purchase's category**, so
the reduction lands where the spending did. A category the user confirmed on an
accepted purchase outranks whatever the parser guessed on the row.
`classification.is_spending_reduction` keeps refunds out of `SPENDING_TYPES` on
purpose: a purchase and its refund touch the same total with opposite signs, and
a caller that could sum them would report a larger total than either.

`propose_refund_matches` searches purchases that are **already accepted** as well
as open ones, because a refund usually arrives weeks after its purchase was
reviewed; requiring both sides to be open would miss almost every real case.

One refund can resemble several purchases at once — the same shop, the same
amount, a fortnight apart. Confirming one pairing dismisses the others for that
refund, so the queue does not keep asking a question the user has answered.

Refunds nothing claims are not hidden. `unmatched_refunds` lists the credit rows
no candidate speaks for, and they stay in the review queue — an unmatched credit
might be a refund whose purchase was never screenshotted, and it might genuinely
be income. Only the user can say which. Rejecting a candidate returns its refund
to that list rather than making the row disappear.

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

### Internal transfers

Money moving between two accounts the same user owns is recorded twice and is
one event. `apps.reconciliation.transfers.confirm_internal_transfer` is the only
way to resolve one, because it is the only path that creates a *single*
`CanonicalTransaction` both observations point at. Confirming a transfer through
the ordinary `confirm_match` would leave each side free to be accepted on its
own, which is exactly the double count the candidate exists to prevent — so that
path refuses the type outright.

The transfer is typed `internal_transfer`, which
`apps.transactions.classification` places in `NEUTRAL_TYPES`: neither spending
nor income. Counting it either way would invent an expense and a payday out of a
move that changed the user's net position by nothing.

Two guards keep an external payment from being read as an internal one:

- **Both sides must be mapped to owned accounts, and to different ones.** Money
  sent to someone else has no arriving row in an account the user owns, so it
  never pairs — an unmapped credit of the same amount on the same day is still
  refused.
- **A side already accepted alone blocks the transfer.** Absorbing it silently
  would leave the transaction it already created counted as spending.

`propose_internal_transfers` scans the rows still waiting for review — mapped to
an owned account, dated, and not yet accepted — and records what it finds.
Re-running it is safe: a pair the reviewer already rejected is left alone.

Detection is strict by default: an exact amount within one day. A reviewer who
already believes two rows are the same move can pass a `TransferTolerance` to
widen the amount and date windows — for a wire fee, or two apps disagreeing
about the date. Everything the tolerant search returns scores below
`STRONG_MATCH_SCORE`, because a pair found only by widening the search is
evidence of the reviewer's intent rather than of the transfer.

## Serving the original screenshot

`DocumentImageView` streams the stored original after an ownership check, with
`Cache-Control: private, no-store`. Screenshots are never served from a public
media directory — a bank statement image must not be reachable by anyone who
guesses a URL.

## Regression scenarios

A reconciliation bug does not look like a crash. It looks like a month that is
twice what it should be, or a transfer between a user's own accounts appearing
as both a payday and a shopping spree. Unit tests of the scoring functions miss
those, because each function is right on its own — what goes wrong is the
arithmetic after several of them agree.

`tests/fixtures/reconciliation/*.json` therefore describes whole cases, and
`apps.reconciliation.fixture_harness` loads them. One scenario states:

- the **accounts and screenshots** involved, and the rows read off each;
- the **candidates** detection must propose, with a score range and the
  reasons that must appear;
- how a **reviewer resolves** them — merge, confirm, or reject;
- the rows accepted on their own afterwards;
- and the **totals** that must come out: canonical events, ledger entries, and
  spending, income, refund, and neutral sums.

The totals are the assertions that matter. `spending_minor` and `refund_minor`
are stated separately rather than netted, so a bug that loses one of them cannot
hide inside the difference.

Every scenario is run four ways: detection matches expectations, **rerunning
detection changes nothing**, the books add up after every decision, and
**repeating every decision changes nothing**. The last two are what catch a
retried worker doubling the month.

A scenario that names a row or account it never declared is refused at load
time. A typo in a row key would otherwise assert nothing and pass.

Fixtures are sanitized by construction: invented amounts and dates, and the same
generic merchant names used throughout the suite. Nothing in them comes from a
real statement.

## Testing

```bash
uv run pytest tests/test_observation_import.py tests/test_review_queue.py
uv run pytest tests/test_review_actions.py tests/test_review_views.py
uv run pytest tests/test_duplicate_detection.py tests/test_reconciliation_matching.py
uv run pytest tests/test_reconciliation_fixtures.py tests/test_idempotency.py
```

Scoring and matching are pure functions over `ObservationFacts`, so they are
tested without a database or a data key. Decryption happens once in the caller.
