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

## Balances (`apps.ledger.balances`)

A balance is derived from the ledger, never read off a screenshot. A bank's
balance line is one institution's opinion at one moment: it is stale the instant
anything else clears, it does not exist on a card list, and two screenshots of
the same account minutes apart disagree. The ledger is every event the user
confirmed, so adding it up gives a figure consistent with the reports by
construction — both read the same rows.

**Direction comes from the account, not the entry.** A debit increases a bank
account and decreases a credit-card liability. Signing every entry the same way
would report a card balance that grows as it is paid off, which is why
`NORMAL_BALANCE_SIGNS` is a table over `(normal_balance, entry_type)` rather
than a sign on the amount. A positive balance means "more of whatever this
account normally holds": money in an asset, debt in a liability.

| Function | Answers |
| --- | --- |
| `account_balances` | Every ledger account's position, one row per account and currency |
| `positions` | Assets, liabilities, and net, per currency |
| `financial_account_balances` | The same, keyed by the account a person recognises |

**Only confirmed transactions count.** A voided one has been withdrawn, and its
entries are cancelled by reversals — but relying on that cancellation would make
the balance depend on the reversal having been written. Filtering on the status
instead means a balance can never be one missed step away from counting money
the user said was never spent.

Equity, income, and expense accounts are excluded from the net position. They
are the other side of the same movements; counting them in would double every
entry.

Currencies are kept apart here for the same reason they are everywhere else:
nothing in this application converts between them, so adding them would invent
an exchange rate.

### Opening balances

An account opened with money already in it has to say so, or every balance the
ledger derives is short by that amount. `apps.financial_accounts.opening_balances`
posts it as a balanced pair against an equity account, on a transaction of type
`opening_balance`.

It is not income. The money was not earned in any period this system covers, and
posting it against equity rather than an income account is what keeps it out of
"what came in this month". `reportable_transactions` then excludes the type
outright — an opening balance did not happen in a month, it is the position the
month started from, so even counting it as movement would report a transfer the
user never made. It is also placed in `NEUTRAL_TYPES`, so if some future report
does read one, the worst it can do is add nothing to either total.

Corrections are adjustments. The original entries are never rewritten: every
balance the system has reported was computed from them, and changing one
silently would leave those figures different today with nothing recording what
they used to be. `correct_opening_balance` posts the difference and requires a
reason.

## Income against spending

`apps.reports.overview` keeps a period's four honest answers apart: what came in,
what went out for good, what merely moved, and what nobody has decided about yet.
Collapsing them into a single "balance" is how a report claims a user earned half
a million won by moving money into their own savings account.

It also **shows the exclusions**. A total that quietly leaves out transfers and
card payments is indistinguishable, to the person reading it, from a total that
lost them to a bug — so `excluded_minor` is a figure on the page, and every
excluded row carries the sentence explaining why it is excluded.

Three distinctions:

- **A transfer is not income.** The same money, somewhere else.
- **A settlement is not spending.** Already counted when it was spent.
- **Cash withdrawn is not cash spent.** A withdrawal moves money from an account
  to a pocket; it becomes spending when it is spent, if a screenshot of that ever
  arrives. The two figures sit side by side and are never added.

Card spending and cash spending are split as well: a purchase with no instrument
attached is cash out of a pocket.

### Adjustments and unknowns

They are counted in **neither** total and shown on their own row with a count.
An adjustment of unknown sign added to spending is a wrong number; one silently
dropped is a total that does not add up. Until the user says what a transaction
was, the honest report is "this much is unclassified".

## Predicates

`apps.reports.predicates` turns the same classification into `Q` objects, so a
report can narrow in the database instead of fetching a month and discarding most
of it. Python and SQL cannot disagree, because both read the same frozensets —
there is no second list of type names anywhere. Type lists are sorted into the
predicate so the generated SQL is identical run to run, which matters when
somebody is reading a query log to work out why a total moved.

A test asserts the predicates partition every transaction type: nothing selected
twice, nothing left out.

The category and merchant breakdowns use them to narrow in SQL. The alternative
fetches a whole month's transfers and card payments only to discard them in
Python, and fetching is the part a report pays for.

### One currency check, in one place

`grouping.amount_in` reads an amount and refuses a row whose `currency` column
contradicts the currency encoded in its amount. Every report goes through it. The
check was written three times before it lived in one place, and a drifting copy
would have had one report refuse a row while another quietly lost it.

## Outstanding work

Every other report answers "what did I spend". `apps.reports.workload` answers the
question that has to come first: **is what I am looking at complete?** A month's
total is only as trustworthy as the pile of unreviewed screenshots behind it, and
a user who cannot see that pile has no way to tell a small month from an
unfinished one.

Four groupings, plus a per-screenshot list:

- **By review status** — waiting, accepted, corrected, rejected, merged.
- **By confidence** — bands, not an average. One row parsed at 0.2 among fifty at
  0.98 is the row that matters, and an average of 0.96 hides it. High-risk rows
  are counted separately, since those refuse acceptance without explicit
  confirmation.
- **By what needs checking** — read straight from `queue_counts`, so this page and
  the review queue can never disagree about how many rows are waiting.
- **By reconciliation status** — proposed, confirmed, dismissed.

**Every count is a link.** A number a user cannot act on tells them they have work
without telling them where it is, which is worse than not showing it: they now
know the total is incomplete and still cannot fix it.

**Rejected rows stay visible.** A rejection is a decision the user made, so it is
shown with the note that it is never counted in a total — and it genuinely cannot
be, because reports read canonical transactions and a rejected row has none.

## Exports

An export is the one point where financial history leaves the encrypted store in
readable form, so three things matter more here than anywhere else.

**Amounts stay in minor units.** `42900`, never `429.00`. KRW has no minor unit,
so a decimal point would be a lie — and a spreadsheet opening the file would round
it again. The currency is on every row so a reader can divide if they want to.

**The field list is fixed and documented** (`EXPORT_FIELDS`). An export whose
columns move between versions cannot be diffed against last month's, which is
most of why people export.

**A plaintext file is temporary.** CSV and JSON exports are deleted an hour after
generation whether or not anybody downloaded them. The reason they exist — so a
person can save the data elsewhere — is finished within minutes; the risk is not.
`purge_expired_exports`, wired to the `purge_expired_exports` management command,
runs without a user, because the file that matters is the one the user forgot
about.

### The encrypted archive

The one form safe to keep. A passphrase is stretched with **Argon2id** (64 MiB,
3 passes) into an AES-256-GCM key; the salt and nonce travel with the ciphertext
and the format header is authenticated as associated data, so an edited header
fails to open rather than being read the wrong way. Two archives of identical data
differ, because the salt and nonce are fresh each time.

The passphrase is never stored. An archive whose passphrase is lost cannot be
opened by anyone, including the user — said on the page rather than discovered
later.

### Getting one

`create_export` requires a **recent sign-in**, measured from `last_login`. An
abandoned session must not be enough to turn a whole financial history into a
file. That is blunt until an explicit re-authentication prompt exists (#175): it
currently means a sign-in within the window rather than a re-entered password.

Downloads resolve through the owner's own queryset, so another user's export is
indistinguishable from one that never existed. The export root is `0700` and each
file `0600`.

The audit log records the format, row count, byte size, and date range — never an
amount, a merchant, or the passphrase.

## Nothing is cached

Every figure on a report page is derived from amounts encrypted per user. A
cached total is a plaintext copy of somebody's finances living outside the
encrypted store, so the report views carry `never_cache` and write nothing of
their own. The pages are cheap to rebuild and expensive to leak, which settles
the trade (specification 22.5).

## What a report costs

The database cannot add encrypted amounts up, so the application decrypts row by
row. The obvious fix is to cache the totals, and that is the one thing this design
cannot afford: a cached total is a plaintext copy of somebody's finances sitting
outside the encrypted store.

So `apps.reports.benchmark` measures rather than guesses, splitting a report into
the three costs that behave differently:

| Stage | What it is | How you fix it |
| --- | --- | --- |
| `query` | Fetching rows | Indexes, narrower predicates |
| `decrypt` | One AES-GCM open per row | Decrypt fewer rows |
| `aggregate` | The arithmetic | Never the problem |

`ReportTimings.dominant_cost` names which one leads, so a slow report says what to
fix instead of inviting a guess.

Each row is decrypted **once**. `accumulate_amounts` exists so the arithmetic can
be timed over amounts that have already been read — a benchmark that decrypted
inside its own "aggregate" stage would charge the cheap stage for the expensive
one and hide exactly what it was built to expose.

### The budget

Milliseconds per 1,000 transactions, enforced by
`tests/test_report_performance.py` over a fixture month of 600 genuinely
encrypted rows in a realistic type mix:

| Stage | Budget |
| --- | --- |
| `query_ms` | 250 |
| `decrypt_ms` | 750 |
| `aggregate_ms` | 100 |
| `total_ms` | 1000 |

Generous on purpose. These numbers decide whether to add a cache, and a tight
budget would argue for one prematurely.

### When a snapshot would be justified

Two conditions, both required (`snapshots_would_help`):

1. **At least 50,000 transactions** in the period. Below that, decrypting per
   request is cheaper than the risk a stored total introduces.
2. **Decryption is the dominant cost.** A slow query is fixed with an index, not
   with a second copy of the data.

Even then a snapshot would have to be encrypted itself, and it would have to be
invalidated by every correction — which is most of what a review queue does. The
bar is deliberately high.

### Correctness under encryption

The same fixture asserts that the buckets *partition* the money: their sum equals
the plain total of every decrypted amount, so a row counted twice or dropped moves
a figure. Amounts are distinct (1000, 1001, 1002…) precisely so that it does.

Totals are also asserted unchanged after re-encrypting every amount under a new
key version — and a month where only *some* rows were rotated raises rather than
reporting a smaller total, because half-reading a month is the failure that would
otherwise look like a quiet month.

## Testing

```bash
uv run pytest tests/test_monthly_spending.py tests/test_category_reports.py
uv run pytest tests/test_activity_reports.py tests/test_income_versus_spending.py
uv run pytest tests/test_workload_report.py tests/test_exports.py
uv run pytest tests/test_report_performance.py
```

The month in `test_a_hand_calculated_month_adds_up` was worked out with a pen
before it was asserted, which is the point: a total computed by the code and then
frozen as the expectation tests nothing.
