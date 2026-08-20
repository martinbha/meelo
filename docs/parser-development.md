# Parser development

This is the contributor checklist for adding an institution parser. The
existing [parser reference](../PARSERS.md) explains the shared primitives and
the fixture harness in detail; this page focuses on the repeatable workflow.

## 1. Define the contract

Before writing code, record the institution's supported screen types, date
context, currency markers, direction labels, balance columns, and card
metadata. Parsers implement `ScreenshotParser` from
`apps/parsing/contracts.py` and expose a stable `ParserMetadata` name and
version. The parser registry uses the name for explicit overrides and the
version is stored on imported observations.

Use the shared helpers instead of local regular expressions:

- `apps.parsing.dates` resolves full, partial, and relative dates and reports
  inference confidence.
- `apps.parsing.money` parses currency-aware amounts into integer minor units.
- `apps.parsing.direction` preserves the printed sign and label while deriving
  debit, credit, or unknown.
- `apps.parsing.rows` groups tokens by their visual coordinates.
- `apps.parsing.balances` checks running-balance arithmetic without changing the
  source amount.

Never guess an ambiguous amount, date, or direction. Return the appropriate
missing or ambiguous field so review is required.

## 2. Add the parser profile

Create `apps/parsing/institutions/<name>.py` with an `InstitutionProfile` and a
parser class, then export the class from
`apps/parsing/institutions/__init__.py`. Keep institution markers specific
enough to avoid selecting the parser for another bank. Put per-row labels in
`layout_markers`; reserve `source_type_markers` for screen titles.

Override a parser hook only when the profile cannot describe the layout. The
registry already handles scoring, explicit overrides, and deterministic
tie-breaking.

## 3. Create sanitized fixtures

Add at least one JSON fixture under
`tests/fixtures/parsers/<institution>/`. Fixtures contain synthetic or
redacted OCR text only—never account numbers, card numbers, names, balances,
screenshots, or OCR copied from a real person. Store raw OCR text and bounding
boxes so the harness exercises normalization and row grouping too.

Each fixture should include:

1. document metadata (`source_type`, dimensions, upload time, and time zone);
2. tokens with `[left, top, right, bottom]` bounds; and
3. hand-checked expected observations, including amount, currency, date,
   direction, and any instrument or settlement metadata.

## 4. Test and measure

Run the focused regression suite first:

```bash
uv run pytest tests/test_institution_parsers.py
```

Then run the full checks used by CI:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

The fixture harness reports amount, date, merchant, direction, metadata,
missed-transaction, and false-positive rates. A parser change is complete only
when all existing fixtures remain clean and the new fixture meets the project
thresholds. Add a regression fixture for every bug found in production.

## 5. Version and review

Bump `InstitutionProfile.version` whenever the same input can produce a
different observation. Bump `PARSER_OUTPUT_VERSION` only when the shape of
`ParsedObservation` changes. Before opening a pull request, check that the
registry selection, manual override path, fixture sanitization, and parser
metadata are covered by tests.

## Checklist

- [ ] Profile markers and source types are documented.
- [ ] Parser is registered and has a meaningful version.
- [ ] Fixtures are sanitized and cover normal plus ambiguous rows.
- [ ] Expected observations are hand-checked.
- [ ] Focused and full checks pass.
- [ ] Accuracy and false-positive thresholds are unchanged or intentionally
      reviewed.
- [ ] The version bump and any migration of fixture expectations are explained.
