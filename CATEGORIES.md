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

The same marker feeds the `prior_confirmation` tier: a manual correction on one
transaction becomes the default for the *next* transaction from that merchant.
One correction, applied forward, without the user having to write a rule.

## Two entry points, deliberately different

- `categorize_transaction` refuses a confirmed transaction. Automatic
  classification must not rewrite history the user has signed off.
- `set_category_manually` accepts one. The category is the part of a confirmed
  row a person is expected to keep refining, and refusing it would leave them
  looking at a total they know is wrong and cannot fix.

Re-applying a decision that matches what is already stored writes nothing, so a
bulk re-run does not churn every row's `updated_at`.

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
