"""Exact-match lookups that use an index rather than reading every row (#162).

Two failure modes, and they look nothing alike.

The loud one is a scan: correct answers, growing slowly, until a report times
out on a year of data. The quiet one is a *missing* token — the lookup runs, the
index is used, and it finds nothing, because the row it should have matched was
written before the column existed or under a different key. Nothing raises.
"No such merchant" is what both a genuinely absent merchant and a broken index
look like.

So these tests check three separate things: the token is written when the row
is, the lookup finds the row, and the query plan says an index was used.
"""

from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connection

from apps.categorization.models import CategoryRule
from apps.categorization.normalization import merchant_blind_index
from apps.categorization.services import COUNTERPARTY_RULE_TYPES, rule_pattern_index
from apps.core.key_management import (
    get_user_data_key,
    get_user_search_key,
    provision_user_data_key,
)
from apps.core.management.commands.backfill_blind_indexes import backfill_user
from apps.core.searchable import (
    approval_code_index,
    counterparty_index,
    identifier_index,
    normalize_approval_code,
    normalize_identifier,
)
from apps.financial_accounts.models import FinancialAccount
from apps.observations.models import ImportedObservation
from apps.transactions.models import CanonicalTransaction
from apps.transactions.services import create_manual_transaction
from tests.factories import make_account, make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db

MERCHANT = "스타벅스 강남점"
COUNTERPARTY = "김대성"


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="lookup-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


@pytest.fixture
def search_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_search_key(user=owner, actor=owner, master_key=master_key)


# ----------------------------------------------------------------------
# Normalization: both sides have to agree, or the lookup finds nothing
# ----------------------------------------------------------------------


def test_an_approval_code_matches_however_the_screen_spelled_it() -> None:
    """A card app and a bank app print one authorisation two ways."""

    assert normalize_approval_code("12-3456") == normalize_approval_code("123456")
    assert normalize_approval_code(" AB 3456 ") == normalize_approval_code("ab-3456")


def test_an_identifier_keeps_only_its_digits() -> None:
    assert normalize_identifier("****1234") == "1234"
    assert normalize_identifier("1234-****-****-5678") == "12345678"
    assert normalize_identifier("(1234)") == "1234"


def test_a_value_that_normalizes_to_nothing_gets_no_token() -> None:
    """An index over "" would match every empty row, which reads as a hit."""

    key = os.urandom(32)
    assert identifier_index("****", user_id=1, key=key) == ""
    assert approval_code_index("  ", user_id=1, key=key) == ""
    assert counterparty_index("", user_id=1, key=key) == ""


def test_domains_do_not_collide() -> None:
    """One string, four columns, four different tokens.

    A merchant called "delta" and a counterparty called "delta" are not the same
    row, and a match in one column must not imply a match in another.
    """

    key = os.urandom(32)
    tokens = {
        merchant_blind_index("delta", user_id=1, key=key),
        counterparty_index("delta", user_id=1, key=key),
        approval_code_index("delta", user_id=1, key=key),
        identifier_index("4200", user_id=1, key=key),
    }

    assert len(tokens) == 4


# ----------------------------------------------------------------------
# The tokens are written when the row is
# ----------------------------------------------------------------------


def test_a_manually_entered_transaction_is_findable(
    owner: Any, data_key: bytes, search_key: bytes
) -> None:
    """Without the token, no alias matches it and no rule ever fires on it."""

    account = make_account(owner)

    transaction = create_manual_transaction(
        user=owner,
        occurred_at=date(2026, 8, 15),
        amount_minor=4_200,
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=account,
        merchant=MERCHANT,
        counterparty=COUNTERPARTY,
        data_key=data_key,
        blind_index_key=search_key,
    )

    assert transaction.merchant_blind_index == merchant_blind_index(
        MERCHANT, user_id=owner.pk, key=search_key
    )
    assert transaction.counterparty_blind_index == counterparty_index(
        COUNTERPARTY, user_id=owner.pk, key=search_key
    )
    found = CanonicalTransaction.objects.filter(
        user=owner, merchant_blind_index=transaction.merchant_blind_index
    )
    assert list(found) == [transaction]


def test_an_imported_observation_indexes_its_approval_code(
    owner: Any, data_key: bytes, search_key: bytes
) -> None:
    from decimal import Decimal

    from apps.observations.services import import_parser_selection
    from apps.parsing.contracts import ParsedObservation, ParserMetadata, ParserSupport
    from apps.parsing.contracts import TransactionDirection as Direction
    from apps.parsing.registry import ParserSelection

    document = make_document(owner, file_sha256="4" * 64)
    run = make_ocr_run(owner, document)
    rows = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (
                ParsedObservation(
                    occurred_on=date(2026, 8, 15),
                    amount=Decimal("4200"),
                    currency="KRW",
                    direction=Direction.DEBIT,
                    merchant=MERCHANT,
                    approval_code="12-3456",
                    confidence_factors={
                        "token_confidence": 0.95,
                        "date_confidence": 1.0,
                        "amount_confidence": 0.98,
                        "direction_confidence": 0.95,
                    },
                    parser_name="toss_bank",
                    parser_version="1.0",
                    parser_support_score=0.95,
                ),
            ),
        ),
        data_key=data_key,
        key_version=1,
        blind_index_key=search_key,
        actor=owner,
    ).observations

    row = rows[0]
    assert row.approval_code_blind_index == approval_code_index(
        "123456", user_id=owner.pk, key=search_key
    )
    # And a lookup by the code as the *other* app printed it still finds it.
    lookup = approval_code_index("123456", user_id=owner.pk, key=search_key)
    assert list(
        ImportedObservation.objects.filter(user=owner, approval_code_blind_index=lookup)
    ) == [row]


# ----------------------------------------------------------------------
# The rule-domain bug
# ----------------------------------------------------------------------


def test_a_counterparty_rule_is_indexed_in_the_counterparty_domain() -> None:
    """Indexed as a merchant, it would be compared against a counterparty token.

    That comparison can never be true, for any input, and nothing would say so.
    """

    key = os.urandom(32)

    counterparty_rule = rule_pattern_index(
        CategoryRule.RuleType.COUNTERPARTY_EXACT, COUNTERPARTY, user_id=1, key=key
    )
    merchant_rule = rule_pattern_index(
        CategoryRule.RuleType.MERCHANT_EXACT, COUNTERPARTY, user_id=1, key=key
    )

    assert counterparty_rule == counterparty_index(COUNTERPARTY, user_id=1, key=key)
    assert merchant_rule == merchant_blind_index(COUNTERPARTY, user_id=1, key=key)
    assert counterparty_rule != merchant_rule


def test_every_counterparty_rule_type_is_covered() -> None:
    assert sorted(COUNTERPARTY_RULE_TYPES) == sorted(
        {
            CategoryRule.RuleType.COUNTERPARTY_EXACT,
            CategoryRule.RuleType.COUNTERPARTY_CONTAINS,
        }
    )


# ----------------------------------------------------------------------
# The backfill
# ----------------------------------------------------------------------


def make_unindexed(owner: Any, data_key: bytes, count: int) -> list[CanonicalTransaction]:
    """Rows as they would look if written before the column was indexed."""

    account = make_account(owner, name_blind_index="lookup-backfill")
    rows = []
    for index in range(count):
        transaction = create_manual_transaction(
            user=owner,
            occurred_at=date(2026, 8, 15),
            amount_minor=1_000 + index,
            currency="KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
            financial_account=account,
            merchant=MERCHANT,
            counterparty=COUNTERPARTY,
            data_key=data_key,
        )
        rows.append(transaction)
    CanonicalTransaction.objects.filter(user=owner).update(
        merchant_blind_index="", counterparty_blind_index=""
    )
    return rows


def test_the_backfill_indexes_rows_written_before_the_column_existed(
    owner: Any, data_key: bytes, search_key: bytes
) -> None:
    rows = make_unindexed(owner, data_key, 5)
    lookup = merchant_blind_index(MERCHANT, user_id=owner.pk, key=search_key)
    assert not CanonicalTransaction.objects.filter(merchant_blind_index=lookup).exists()

    report = backfill_user(user=owner, data_key=data_key, search_key=search_key, batch_size=2)

    assert report.is_clean
    assert report.tokens_written == 2 * len(rows)
    assert CanonicalTransaction.objects.filter(merchant_blind_index=lookup).count() == len(rows)


def test_the_backfill_writes_nothing_on_a_second_run(
    owner: Any, data_key: bytes, search_key: bytes
) -> None:
    """A cron entry that overlaps itself must not be a problem."""

    make_unindexed(owner, data_key, 3)
    backfill_user(user=owner, data_key=data_key, search_key=search_key, batch_size=2)

    again = backfill_user(user=owner, data_key=data_key, search_key=search_key, batch_size=2)

    assert again.tokens_written == 0
    assert again.rows_examined > 0


def test_an_interrupted_backfill_resumes_without_redoing_finished_rows(
    owner: Any, data_key: bytes, search_key: bytes
) -> None:
    rows = make_unindexed(owner, data_key, 6)
    lookup = merchant_blind_index(MERCHANT, user_id=owner.pk, key=search_key)

    # Stand in for an interruption: index the first three by hand, then run.
    for transaction in sorted(rows, key=lambda row: row.pk)[:3]:
        transaction.merchant_blind_index = lookup
        transaction.counterparty_blind_index = counterparty_index(
            COUNTERPARTY, user_id=owner.pk, key=search_key
        )
        transaction.save(update_fields=["merchant_blind_index", "counterparty_blind_index"])

    report = backfill_user(user=owner, data_key=data_key, search_key=search_key, batch_size=2)

    assert report.tokens_written == 2 * 3
    assert CanonicalTransaction.objects.filter(merchant_blind_index=lookup).count() == len(rows)


def test_a_dry_run_reports_without_writing(owner: Any, data_key: bytes, search_key: bytes) -> None:
    make_unindexed(owner, data_key, 3)

    report = backfill_user(
        user=owner, data_key=data_key, search_key=search_key, batch_size=2, dry_run=True
    )

    assert report.tokens_written == 6
    assert not CanonicalTransaction.objects.exclude(merchant_blind_index="").exists()


def test_the_command_runs_for_one_user(
    owner: Any, data_key: bytes, search_key: bytes, capsys: Any
) -> None:
    make_unindexed(owner, data_key, 2)

    call_command("backfill_blind_indexes", email=owner.email)

    captured = capsys.readouterr()
    assert owner.email in captured.out
    assert "wrote" in captured.out


def test_the_backfill_never_reaches_another_users_rows(
    owner: Any, data_key: bytes, search_key: bytes, master_key: bytes
) -> None:
    stranger = make_user(email="lookup-stranger@example.com")
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)
    their_key = get_user_data_key(user=stranger, actor=stranger, master_key=master_key)
    theirs = create_manual_transaction(
        user=stranger,
        occurred_at=date(2026, 8, 15),
        amount_minor=9_900,
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=make_account(stranger, name_blind_index="lookup-theirs"),
        merchant=MERCHANT,
        data_key=their_key,
    )
    make_unindexed(owner, data_key, 2)

    backfill_user(user=owner, data_key=data_key, search_key=search_key)

    theirs.refresh_from_db()
    assert theirs.merchant_blind_index == ""


# ----------------------------------------------------------------------
# The plan
# ----------------------------------------------------------------------


def explain(queryset: Any) -> str:
    sql, params = queryset.query.sql_with_params()
    prefix = "EXPLAIN QUERY PLAN " if connection.vendor == "sqlite" else "EXPLAIN "
    with connection.cursor() as cursor:
        cursor.execute(prefix + sql, params)
        return "\n".join(str(row) for row in cursor.fetchall()).lower()


@pytest.mark.parametrize(
    ("model", "column"),
    [
        (CanonicalTransaction, "merchant_blind_index"),
        (CanonicalTransaction, "counterparty_blind_index"),
        (ImportedObservation, "merchant_blind_index"),
        (ImportedObservation, "approval_code_blind_index"),
        (FinancialAccount, "institution_blind_index"),
        (FinancialAccount, "identifier_blind_index"),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_every_lookup_pair_has_an_index_behind_it(model: Any, column: str) -> None:
    pairs = {
        tuple(index.fields)
        for index in model._meta.indexes
        if tuple(index.fields) == ("user", column)
    }
    unique_pairs = {
        tuple(constraint.fields)
        for constraint in model._meta.constraints
        if getattr(constraint, "fields", None) and column in constraint.fields
    }
    assert pairs or unique_pairs, f"{model.__name__}.{column} has no (user, column) index."


def seed_many(owner: Any, account: Any, *, count: int, target: str) -> None:
    """Enough rows that a planner has a reason to prefer the index.

    Written with ``bulk_create`` and no encryption, because what is under test
    is the plan for a lookup, not the write path — and a few thousand real
    encrypted inserts would make this test slow enough that somebody would
    eventually delete it.

    Every row gets a distinct token except one, so the value being searched for
    is as selective as a real merchant lookup. Seeding two thousand identical
    tokens would give the planner a good reason to scan, and the test would then
    be measuring the seed rather than the index.
    """

    rows = [
        CanonicalTransaction(
            user=owner,
            created_by=owner,
            financial_account=account,
            occurred_at=date(2026, 8, 1 + index % 28),
            amount_encrypted=f"{1000 + index}:KRW",
            currency="KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
            merchant_blind_index=f"1:{index:064x}",
        )
        for index in range(count)
    ]
    rows[0].merchant_blind_index = target
    CanonicalTransaction.objects.bulk_create(rows, batch_size=500)


def test_the_query_plan_uses_the_index_on_a_realistic_row_count(
    owner: Any, search_key: bytes
) -> None:
    """The acceptance criterion, on enough rows for the planner to have a choice.

    On forty rows PostgreSQL scans, and it is right to — the whole table fits in
    a page. A test that passed there would be asserting nothing about what
    happens on a year of transactions.
    """

    account = make_account(owner, name_blind_index="lookup-plan")
    target = merchant_blind_index(MERCHANT, user_id=owner.pk, key=search_key)
    seed_many(owner, account, count=2_000, target=target)
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            # Without fresh statistics the planner is guessing at selectivity.
            cursor.execute("ANALYZE transactions_canonicaltransaction")

    # ``order_by()`` clears the model's default ordering. A lookup asks "which
    # rows match this token", and the sort the default ordering adds is what
    # tips the planner towards a scan for reasons that have nothing to do with
    # the index under test.
    plan = explain(
        CanonicalTransaction.objects.filter(user=owner, merchant_blind_index=target).order_by()
    )

    assert "transaction_merchant_idx" in plan, plan
    if connection.vendor == "postgresql":
        assert "index scan" in plan or "bitmap" in plan, plan
        assert "seq scan" not in plan, plan
