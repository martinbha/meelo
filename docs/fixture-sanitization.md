# Fixture sanitization

Committed fixtures must be synthetic. Never copy a real screenshot or extracted
financial record into the repository, even if it belongs to you or appears to be
partially masked.

## Procedure

1. Recreate the minimum layout or token sequence needed by the test. Do not edit a
   real screenshot in place because image metadata and overlooked pixels can still
   disclose data.
2. Replace every person's name with an invented name. Replace account, card, phone,
   customer, transaction, and other identifiers with invented values, then mask all
   but the final four digits (for example, `110-***-4567`).
3. Replace balances, transaction amounts, dates, and approval codes with synthetic
   values. Approval codes must not be copied from a real transaction.
4. Use invented merchant names or clearly generic test merchants. Do not retain a
   merchant/date/amount combination from a real purchase.
5. Check expected parser output as carefully as the input tokens; sensitive values
   are often duplicated there.
6. Run `uv run python scripts/check_fixture_sanitization.py` and the relevant fixture
   tests. Review the complete diff manually before committing.

The automated check is intentionally only a backstop for obvious unmasked account
identifiers. Passing it does not prove that a fixture is safe to publish.
