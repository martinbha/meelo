# Categorization Guide

How a transaction gets a category, and why the order matters more than any
individual rule.

## The precedence

Categorization is a stack of increasingly general guesses. `classify` tries them
in a fixed order and the first hit wins (specification 18):

| Source | What it is | Why it sits here |
| --- | --- | --- |
| `manual_override` | The user said so on this row | Nothing the system infers can outrank what the user stated |
| `user_rule` | An exact merchant rule they wrote | A written rule is a deliberate policy |
| `card_rule` | "Everything on this card is X" | Deliberate, but broader than a merchant rule |
| `merchant_alias` | A learned name carrying a default | The system's own generalisation |
| `counterparty_rule` | Keyed on who was paid | Narrower evidence than what was bought |
| `prior_confirmation` | What they chose last time this merchant appeared | A decision, but about one row rather than a policy |
| `parser` | Whatever the parser guessed at import | The weakest evidence there is |
| `uncategorized` | Nothing applied | The honest answer |

**Uncategorized is a result, not a failure.** A wrong category that looks
confident is worse than an empty one, because only the empty one gets fixed.

## Every decision names its source

`CategoryDecision` carries the category, the source, and the identifier of
whatever produced it. `category_source` is then stored on the transaction rather
than recomputed, for two reasons: a category nobody can explain cannot be argued
with, and the user is the one who has to argue with it — and the stored source is
what stops re-classification from overwriting a correction.

## Manual corrections are never overwritten

`set_category_manually` writes `manual_override`. `classify` sees that and
returns the stored category without consulting a single rule. Re-running
classification across a user's history would otherwise undo their work on a
schedule.

That holds when the answer was "none of these". Clearing a category by hand is
recorded as a manual override too, so the next run does not quietly re-file the
row into the category the user just removed.

The same marker feeds the `prior_confirmation` tier: a manual correction on one
transaction becomes the default for the *next* transaction from that merchant.
One correction, applied forward, without the user having to write a rule.

A category the parser guessed at import is recorded as `parser` when the
transaction is created, not left blank. A report asking for uncategorised rows
must not count a row that has a category, and the engine has to know how weak
that evidence is before it decides whether to replace it.

## Two entry points, deliberately different

- `categorize_transaction` refuses a confirmed transaction. Automatic
  classification must not rewrite history the user has signed off.
- `set_category_manually` accepts one. The category is the part of a confirmed
  row a person is expected to keep refining, and refusing it would leave them
  looking at a total they know is wrong and cannot fix.

Re-applying a decision that matches what is already stored writes nothing, so a
bulk re-run does not churn every row's `updated_at`.

## Merchant normalization

The same shop reaches this system under several names. A card app prints
`(주)스타벅스코리아 강남점`, the bank prints `스타벅스강남`, and OCR turns one of
them into `스타벅스 강남 점`. A rule the user wrote once has to fire on all
three.

`normalize_merchant` is what makes that work, and it is deliberately lossy. It
strips, in order: compatibility forms (NFKC), case, company forms (`주식회사`,
`(주)`, `ltd`), payment prefixes the statement line carries (`체크카드`, `승인`),
long digit runs that are approval numbers rather than names, punctuation, all
spacing, and finally a trailing branch marker (`점`, `지점`, `본점`). A branch is
a place, not a merchant.

Spacing goes because it is the least reliable part of an OCR'd Korean name —
`스타벅스 강남 점` and `스타벅스강남점` are the same shop and differ only in where
the engine decided a word ended.

**What normalization must never do is replace the name the user sees.** The raw
text is stored separately in `merchant_raw_encrypted` and shown unchanged;
`display_merchant` tidies spacing and nothing else. Showing the normalized form
would tell someone their coffee came from `스타벅스강남` when the receipt says
otherwise.

Normalization cannot know that `스타벅스코리아` and `스타벅스` are one company —
that is a judgement, and judgements are what `MerchantAlias` records.

## Matching in two tiers

- **Exact**, on an HMAC blind index of the normalized form. One query, no
  decryption, and the index reveals nothing: it is keyed and scoped per user, so
  an attacker holding the database can neither confirm a guess by hashing a name
  nor tell that two people shop at the same place.
- **Fuzzy**, in application memory only. `suggest_merchant_aliases` decrypts the
  user's own aliases and ranks them with `rapidfuzz`. Scoring in the database
  would mean putting merchant plaintext there, which is the thing this system
  exists not to do.

Uncertain matches are returned rather than filtered out. A weak suggestion a
reviewer can see is one they can correct; one that was silently discarded is a
merchant that stays unlinked forever.

> **Changing the normalization rules changes every blind index derived from
> them.** Stored indexes do not update themselves, so a change here needs a
> reindex (#168).

## What the blind index can and cannot do

Rules are matched on HMAC blind indexes, so the engine finds a rule without
decrypting every merchant in the database. That bounds what it can express: an
index supports equality and nothing else.

`merchant_contains` and the other pattern rule types exist in `RuleType` but are
not evaluated here — they need plaintext to run against, which is the work in
#191. Matching them as though they were exact would be worse than not matching
them: the user would write a substring rule, watch it fire on one exact match,
and conclude it worked.

## Testing

```bash
uv run pytest tests/test_categorization_engine.py tests/test_merchant_rules.py
```
