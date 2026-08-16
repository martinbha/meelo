# Parser Development Guide

This guide covers adding and changing the screenshot parsers in `apps/parsing`.
It assumes the OCR pipeline already produces normalized tokens; see
specification sections 14, 15, and 31.3 for the requirements behind it.

## What a parser receives

`ScreenshotParser.parse` is handed a `DocumentMetadata` and a sequence of
`NormalizedToken`. The tokens have already been through
`apps.ocr.normalization.normalize_ocr_text`, which means:

- Money with a visible marker is rewritten (`42,900원` becomes `42900 KRW`).
- Fully written dates become ISO (`2026.08.15` becomes `2026-08-15`).
- Everything else is whitespace-collapsed and **casefolded**, so Latin-script
  merchant names arrive lowercase. Merchant casing is restored later by the
  merchant normalization work, not by parsers.

Partial dates (`08.15`), Korean dates (`8월 5일`), relative dates (`오늘`), and
unmarked amounts survive normalization untouched and are the parsers' job.

`DocumentMetadata` also carries the dating context: `uploaded_at`, `time_zone`,
and an optional `statement_month`. Without `uploaded_at`, only explicit full
dates can be resolved — partial dates are left for review rather than guessed.

## The parsing primitives

Build on these rather than writing new regular expressions:

| Module | Use it for |
| --- | --- |
| `apps.parsing.dates` | Resolving any date, with an inference reason and confidence |
| `apps.parsing.money` | Reading amounts into integer minor units |
| `apps.parsing.direction` | Turning labels and signs into a debit/credit/unknown |
| `apps.parsing.balances` | Checking `previous + signed amount == next` |
| `apps.parsing.rows` | Grouping tokens into visual rows by coordinate |

Two rules run through all of them:

- **Never guess.** An amount that reads two ways returns `ambiguous=True` with
  `money=None`. An unresolvable date returns `None`. An unreadable direction is
  `TransactionDirection.UNKNOWN`, which blocks automatic confirmation.
- **Never rewrite the source.** A failed balance check reports the difference;
  it does not correct the amount. The printed sign and label are preserved on
  the observation next to the interpreted direction.

## Adding an institution

Most institutions need only a profile. Create
`apps/parsing/institutions/<name>.py`:

```python
from .base import InstitutionParser, InstitutionProfile

MY_BANK_PROFILE = InstitutionProfile(
    name="my_bank",
    version="1.0",
    display_name="마이뱅크",
    institution_markers=("마이뱅크", "my bank"),  # at least one must appear
    layout_markers=("거래내역", "잔액"),  # confirms a known layout
    chrome_markers=("조회기간", "더보기"),  # rows that carry no transaction
    source_type_markers={  # screen titles, most specific first
        "bank_transaction_detail": ("거래상세",),
        "bank_transaction_list": ("거래내역",),
    },
    balance_column=True,  # rightmost amount on a row is a running balance
    rows_newest_first=True,  # newest transaction drawn at the top
)


class MyBankParser(InstitutionParser):
    profile = MY_BANK_PROFILE
```

Then register the class in `apps/parsing/institutions/__init__.py`. The pipeline
picks it up through `apps.ocr.pipeline.build_parser_registry`.

Override a hook on the class only when the profile cannot express the layout —
for example `extract_row` for an unusual column order, or `merge_rows` for a
receipt shape the defaults get wrong.

### Marker choice matters

- `institution_markers` gate the parser: no match means a support score of zero
  and the generic parser handles the screenshot.
- `source_type_markers` are treated as screen titles. They are excluded from
  merchant text and from direction labels, because a title like `이체완료`
  contains a direction word that does not describe any single row. Never put a
  per-row label such as `청구금액` here; put it in `layout_markers`.

## Fixtures and the regression suite

Every institution must ship at least one fixture; `test_institution_parsers.py`
fails if one is missing. Fixtures live in
`tests/fixtures/parsers/<institution>/<case>.json` and store **raw** OCR text —
the harness normalizes it, so the fixtures exercise the normalization layer too.

```json
{
  "name": "my-bank-transaction-list",
  "parser": "my_bank",
  "expected_source_type": "bank_transaction_list",
  "minimum_confidence": 0.7,
  "document": {
    "source_type": "bank_transaction_list",
    "width": 1080,
    "height": 1920,
    "uploaded_at": "2026-08-16T12:00:00+09:00",
    "time_zone": "Asia/Seoul"
  },
  "tokens": [
    {"text": "마이뱅크", "bounds": [40, 60, 220, 100]},
    {"text": "2026.08.15", "bounds": [40, 260, 240, 298]},
    {"text": "스타벅스", "bounds": [260, 260, 460, 298]},
    {"text": "출금", "bounds": [600, 260, 680, 298]},
    {"text": "4,200원", "bounds": [700, 260, 860, 298]}
  ],
  "expected": [
    {
      "date": "2026-08-15",
      "merchant": "스타벅스",
      "amount_minor": 4200,
      "currency": "KRW",
      "direction": "debit"
    }
  ]
}
```

`bounds` are `[left, top, right, bottom]`. Coordinates carry meaning: row
grouping uses vertical centres, and direction can be read from an amount's
column when a row prints no label, so keep amounts under their headers.

Sanitize before committing. Fixtures must contain no real account numbers, card
numbers, names, or balances.

Run the suite with:

```bash
uv run pytest tests/test_institution_parsers.py
```

It reports amount, date, merchant, direction, and metadata accuracy plus the
missed and false transaction rates for every fixture, as section 31.3 requires.
The thresholds are exact because fixtures are synthetic — if a change lowers a
number, either the change is wrong or the fixture's expectation needs an
explicit, reviewed update.

## Versioning

Bump `InstitutionProfile.version` whenever a parser's output changes for the
same input. The version is stored on every observation, so reprocessing can
tell which rows came from which parser. `PARSER_OUTPUT_VERSION` in
`apps/parsing/contracts.py` is separate: bump it when the shape of
`ParsedObservation` itself changes.
