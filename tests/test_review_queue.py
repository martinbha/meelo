import os
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from apps.observations.models import ImportedObservation
from apps.observations.queue import (
    QueueFilter,
    open_observations,
    queue_counts,
    review_queue,
)
from apps.observations.services import import_parser_selection, rescore_observation
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from tests.factories import make_account, make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner() -> Any:
    return make_user(email="queue-owner@example.com")


def parsed(**overrides: Any) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 8, 15),
        "amount": Decimal("4200"),
        "currency": "KRW",
        "direction": TransactionDirection.DEBIT,
        "merchant": "스타벅스",
        "confidence_factors": {
            "token_confidence": 0.95,
            "date_confidence": 1.0,
            "amount_confidence": 0.98,
            "direction_confidence": 0.95,
        },
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def seed(user: Any, *observations: ParsedObservation, sha: str = "1" * 64) -> Any:
    document = make_document(user, file_sha256=sha)
    run = make_ocr_run(user, document)
    return import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            observations,
        ),
        data_key=os.urandom(32),
        key_version=1,
    ).observations


def test_every_imported_observation_stays_visible_until_actioned(owner: Any) -> None:
    rows = seed(owner, parsed(), parsed(merchant="두번째"))

    assert open_observations(owner).count() == 2

    rows[0].review_status = ImportedObservation.ReviewStatus.ACCEPTED
    rows[0].save(update_fields=["review_status"])
    assert open_observations(owner).count() == 1

    rows[1].review_status = ImportedObservation.ReviewStatus.REJECTED
    rows[1].save(update_fields=["review_status"])
    assert open_observations(owner).count() == 0


def test_high_risk_rows_sort_ahead_of_routine_ones(owner: Any) -> None:
    rows = seed(
        owner,
        parsed(),  # clean row
        parsed(merchant="두번째", ambiguous_fields=frozenset({"amount"})),
        parsed(merchant="세번째", missing_fields=frozenset({"merchant"})),
    )
    # Map every row so the intrinsic flags, not the missing mapping, decide.
    account = make_account(owner)
    for row in rows:
        row.financial_account_guess = account
        row.save(update_fields=["financial_account_guess"])
        rescore_observation(row)

    page = review_queue(owner)

    assert [item.observation.row_index for item in page.items] == [1, 2, 0]
    assert page.items[0].is_high_risk is True
    assert "ambiguous_amount" in page.items[0].reasons
    assert page.items[-1].risk_score == 0


def test_mapping_a_row_lowers_its_stored_risk(owner: Any) -> None:
    rows = seed(owner, parsed())
    blocked = rows[0].risk_score

    rows[0].financial_account_guess = make_account(owner)
    rows[0].save(update_fields=["financial_account_guess"])
    rescore_observation(rows[0])

    rows[0].refresh_from_db()
    assert blocked == 75
    assert rows[0].risk_score == 0


def test_risk_ordering_spans_pages_rather_than_sorting_within_one(owner: Any) -> None:
    # The risky row is imported last, so a queue that only sorted within a page
    # would leave it stranded on page two.
    rows = [parsed(merchant=f"row-{index}") for index in range(4)]
    rows.append(parsed(merchant="위험", ambiguous_fields=frozenset({"amount"})))
    seed(owner, *rows)

    first_page = review_queue(owner, page_size=2)

    assert first_page.page_count == 3
    assert first_page.items[0].observation.row_index == 4
    assert first_page.items[0].is_high_risk is True


def test_filters_narrow_the_queue(owner: Any) -> None:
    seed(
        owner,
        parsed(),
        parsed(merchant="두번째", ambiguous_fields=frozenset({"amount"})),
        parsed(
            merchant="세번째",
            confidence_factors={"token_confidence": 0.4, "amount_confidence": 0.4},
        ),
        parsed(
            merchant="네번째",
            confidence_factors={"token_confidence": 0.9, "balance_status": "invalid"},
        ),
    )

    assert review_queue(owner, filters=[QueueFilter.AMOUNT_DISAGREEMENT]).total == 1
    assert review_queue(owner, filters=[QueueFilter.BALANCE_MISMATCH]).total == 1
    assert review_queue(owner, filters=[QueueFilter.LOW_CONFIDENCE]).total >= 1
    # Nothing has been mapped to an account or card yet.
    assert review_queue(owner, filters=[QueueFilter.UNKNOWN_MAPPING]).total == 4


def test_mapped_rows_leave_the_unknown_mapping_filter(owner: Any) -> None:
    rows = seed(owner, parsed())
    account = make_account(owner)
    rows[0].financial_account_guess = account
    rows[0].save(update_fields=["financial_account_guess"])

    assert review_queue(owner, filters=[QueueFilter.UNKNOWN_MAPPING]).total == 0


def test_reconciliation_candidates_raise_a_row_to_the_top(owner: Any) -> None:
    rows = seed(owner, parsed(), parsed(merchant="두번째"))
    match_ids = {QueueFilter.DUPLICATE.value: [rows[1].pk]}

    page = review_queue(owner, match_ids=match_ids)

    assert page.items[0].observation.pk == rows[1].pk
    assert "duplicate_candidate" in page.items[0].reasons
    assert page.counts[QueueFilter.DUPLICATE.value] == 1
    assert review_queue(owner, filters=[QueueFilter.DUPLICATE], match_ids=match_ids).total == 1


def test_counts_never_include_another_users_data(owner: Any) -> None:
    seed(owner, parsed(), parsed(merchant="두번째"))
    intruder = make_user(email="intruder-queue@example.com")
    seed(intruder, parsed(merchant="남의것"), sha="2" * 64)

    owner_counts = queue_counts(owner)
    intruder_counts = queue_counts(intruder)

    assert owner_counts["open"] == 2
    assert intruder_counts["open"] == 1
    assert review_queue(owner).total == 2
    assert all(item.observation.user_id == owner.pk for item in review_queue(owner).items)


def test_an_anonymous_user_sees_an_empty_queue(owner: Any) -> None:
    seed(owner, parsed())

    class Anonymous:
        pk = None
        is_authenticated = False

    assert queue_counts(Anonymous())["open"] == 0
    assert review_queue(Anonymous()).total == 0


def test_pagination_reports_navigation_state(owner: Any) -> None:
    seed(owner, *[parsed(merchant=f"row-{index}") for index in range(5)])

    first = review_queue(owner, page_size=2, page_number=1)
    last = review_queue(owner, page_size=2, page_number=3)

    assert first.total == 5
    assert first.page_count == 3
    assert first.has_previous is False and first.has_next is True
    assert last.page_number == 3
    assert last.has_next is False
    assert len(last.items) == 1


def test_page_size_is_bounded(owner: Any) -> None:
    seed(owner, parsed())

    assert len(review_queue(owner, page_size=0).items) == 1
    assert review_queue(owner, page_size=10_000).page_count == 1
