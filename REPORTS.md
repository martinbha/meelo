# Reporting Guide

What a month cost, and what it only looked like it cost.

## Spending is not "money that left the account"

Most of what leaves a bank account in a month is not spending:

- it moved to the user's own savings,
- it paid off a card whose purchases were already counted when they were made,
- or it came out of a machine and is still in their pocket.

Adding those up produces a figure roughly double the truth that looks entirely
plausible. So `apps.reports.spending` works from transaction **types**, never
from directions or from what left which account (specification 2.3, 25.1–25.2).

## Every type is in exactly one bucket

`apps.transactions.classification` places all twelve transaction types:

| Bucket | Types | Effect on the month |
| --- | --- | --- |
| `spending` | purchase, fee, interest | Adds |
| `refund` | refund | Subtracts from spending |
| `income` | income | Adds to income |
| `neutral` | internal transfer, bank transfer, credit-card payment, cash withdrawal, loan payment | Nothing |
| `unresolved` | adjustment, unknown | Counted apart |

A type missing from all five would vanish from every total without anything
saying so; a type in two would be counted twice. A completeness test holds that
shut, and `bucket_of` raises on a type it does not know rather than ignoring it.

**Refunds are kept out of `spending` on purpose.** A purchase and its refund
touch the same total with opposite signs, so a caller able to sum both would
report a figure larger than either. `net_spending_minor` is the only place the
subtraction happens.

**Adjustments and unknowns get their own bucket.** An adjustment of unknown sign
added to spending is a wrong number; one silently dropped is a total that does
not add up. Reporting shows them separately (#87).

## The two rules that carry the weight

- **A credit-card purchase counts once, when it is bought.** The payment that
  settles the statement counts zero. Counting both would double every card
  purchase in the month.
- **A refund reduces the category it came from.** It is not income — the user is
  back where they started, not better off than they began.

## What a report reads

`reportable_transactions` returns `CanonicalTransaction` rows in `draft` or
`confirmed` status, scoped to their owner.

- **Observations never appear.** A row a reviewer has not accepted, or has
  rejected, has no canonical transaction, so it cannot reach a total by any path.
- **Voided transactions are excluded.** That is history the user withdrew.
- **Draft transactions are included.** A person accepted them; posting to the
  ledger is a separate step, and a month that ignored unposted acceptances would
  disagree with the review queue.

## Currencies are never added together

Totals are kept per currency, and `MonthlySpending.totals(code)` returns an
empty set rather than raising for a currency with no activity. A total that
mixes two currencies is a number nobody can trace back, so the shape of the
result makes it impossible rather than merely discouraged.

A row whose `currency` column disagrees with the currency encoded in its amount
raises. There is no honest answer to which one it is in, and filing it under
either would put a real number into a total it does not belong to — while a
later query filtering on the column would disagree with this one.

## Amounts

Stored as `minor_units:CURRENCY` — integer minor units, never a float and never a
major-unit decimal, so a report ends on the same number a person would reach with
a pen. Rounding happens once, when a parser reads a screen, and never again.

`transaction_amount` recognises an encrypted amount by its envelope prefix rather
than by attempting a decrypt and catching the failure, and **raises** when a row
is encrypted and no key was given. A silently skipped row shrinks a month in the
one direction nobody checks.

## Category and merchant breakdowns

The database cannot add encrypted amounts up — `SUM()` over a column of
ciphertext is not a number — so the arithmetic happens in the application
process, over rows decrypted one at a time and discarded.

Grouping by merchant has the same shape of problem: the name is encrypted, so
rows are grouped on their **blind index**, which is queryable, and exactly one
representative name per group is decrypted for the label.

Only the `spending` and `refund` buckets reach a line. Income and pure movement
belong to the income-versus-spending view (#87); mixing them in would break the
reconciliation the breakdown promises.

**Uncategorised is a line, sorted last.** Money that fell out of every category
is the first thing a user should see and the last thing to bury mid-list — it is
a call to action, not a category.

A `Breakdown` deliberately does **not** carry a `SpendingTotals`. Only two of
its five buckets apply, and handing back one with `income_minor` sitting at zero
would invite a caller to conclude there was no income — when income was never in
scope. It reports its own gross, refunds, net, and count instead.

`reconciles(breakdown, totals)` compares the lines against a total computed
independently by `monthly_spending`. A breakdown checked only against its own
sums checks nothing. It returns rather than raises: a disagreement has to be
visible on the page, not an error in front of someone who only wanted to look at
their month. The page only claims reconciliation when it is showing a whole
unfiltered month, because a narrowed range is *expected* to differ.

## Account and card activity

`apps.reports.activity` answers "what happened on this card" without the double
count that would make the answer useless. Two dangers, both specific:

- **One purchase, two screenshots.** A debit-card purchase appears in the card
  app and again in the bank app. Reconciliation merges those, and reports read
  only `CanonicalTransaction`, so a merged pair contributes once however many
  screenshots it came from.
- **A card payment is not card spending.** It gets its own `settlements_minor`
  column rather than being folded into the spending figure *or* hidden among
  other movement — "you paid your card 380,000" is a number a person goes
  looking for, and "you moved 380,000 around" is not.

`SETTLEMENT_TYPES` (credit-card payment, loan payment) is a sub-classification of
`neutral`, not a bucket of its own: a settlement really is neutral, and giving it
a bucket would have broken the completeness invariant. `settlements_minor +
movement_minor` equals the month's `neutral_minor`, which the tests assert.

**Activity with no card is a line, sorted last.** Unmapped activity is the thing
a user needs to go and map, so it is named rather than dropped.

## Nothing is cached

Every figure on a report page is derived from amounts encrypted per user. A
cached total is a plaintext copy of somebody's finances living outside the
encrypted store, so the report views carry `never_cache` and write nothing of
their own. The pages are cheap to rebuild and expensive to leak, which settles
the trade (specification 22.5).

## Testing

```bash
uv run pytest tests/test_monthly_spending.py tests/test_category_reports.py
uv run pytest tests/test_activity_reports.py
```

The month in `test_a_hand_calculated_month_adds_up` was worked out with a pen
before it was asserted, which is the point: a total computed by the code and then
frozen as the expectation tests nothing.
