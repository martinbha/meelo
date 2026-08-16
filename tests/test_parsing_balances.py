from apps.core.value_objects import Money
from apps.parsing.balances import (
    BalanceRow,
    BalanceStatus,
    apply_confidence,
    validate_balance,
    validate_balance_chain,
)


def krw(amount: int) -> Money:
    return Money(amount, "KRW")


def test_valid_chains_raise_confidence() -> None:
    validation = validate_balance(
        previous_balance=krw(1_000_000),
        signed_amount_minor=-42_900,
        next_balance=krw(957_100),
    )

    assert validation.status is BalanceStatus.VALID
    assert validation.confidence_delta > 0
    assert validation.difference_minor == 0
    assert validation.requires_review is False
    assert apply_confidence(0.8, validation) > 0.8


def test_deposits_validate_with_a_positive_amount() -> None:
    validation = validate_balance(
        previous_balance=krw(957_100),
        signed_amount_minor=3_000_000,
        next_balance=krw(3_957_100),
    )

    assert validation.status is BalanceStatus.VALID


def test_invalid_chains_report_the_difference_without_altering_amounts() -> None:
    validation = validate_balance(
        previous_balance=krw(1_000_000),
        signed_amount_minor=-42_900,
        next_balance=krw(957_000),
    )

    assert validation.status is BalanceStatus.INVALID
    assert validation.difference_minor == -100
    assert validation.expected_next == krw(957_100)
    assert validation.observed_next == krw(957_000)
    assert validation.requires_review is True
    assert validation.confidence_delta < 0
    assert apply_confidence(0.9, validation) < 0.9


def test_missing_balances_are_allowed_without_failure() -> None:
    assert (
        validate_balance(previous_balance=None, signed_amount_minor=-1, next_balance=krw(1)).status
        is BalanceStatus.UNAVAILABLE
    )
    assert (
        validate_balance(previous_balance=krw(1), signed_amount_minor=-1, next_balance=None).status
        is BalanceStatus.UNAVAILABLE
    )
    missing_amount = validate_balance(
        previous_balance=krw(1), signed_amount_minor=None, next_balance=krw(1)
    )
    assert missing_amount.status is BalanceStatus.UNAVAILABLE
    assert missing_amount.requires_review is False
    assert missing_amount.is_checked is False
    assert apply_confidence(0.7, missing_amount) == 0.7


def test_mixed_currencies_are_not_treated_as_a_digit_error() -> None:
    validation = validate_balance(
        previous_balance=Money(1000, "USD"),
        signed_amount_minor=-100,
        next_balance=krw(900),
    )

    assert validation.status is BalanceStatus.UNAVAILABLE
    assert validation.requires_review is False


def test_chains_validate_each_row_against_the_previous_balance() -> None:
    validations = validate_balance_chain(
        (
            BalanceRow(-42_900, krw(957_100), balance_before=krw(1_000_000)),
            BalanceRow(3_000_000, krw(3_957_100)),
            BalanceRow(-7_100, krw(3_950_000)),
        )
    )

    assert [item.status for item in validations] == [BalanceStatus.VALID] * 3


def test_a_chain_isolates_the_row_that_breaks_it() -> None:
    validations = validate_balance_chain(
        (
            BalanceRow(-42_900, krw(957_100), balance_before=krw(1_000_000)),
            BalanceRow(3_000_000, krw(3_957_000)),  # digit error in the balance
            BalanceRow(-7_100, krw(3_949_900)),
        )
    )

    assert validations[0].status is BalanceStatus.VALID
    assert validations[1].status is BalanceStatus.INVALID
    # Later rows chain from the printed balance, so the error does not cascade.
    assert validations[2].status is BalanceStatus.VALID


def test_a_chain_without_an_opening_balance_leaves_the_first_row_unchecked() -> None:
    validations = validate_balance_chain(
        (
            BalanceRow(-42_900, krw(957_100)),
            BalanceRow(-7_100, krw(950_000)),
        )
    )

    assert validations[0].status is BalanceStatus.UNAVAILABLE
    assert validations[1].status is BalanceStatus.VALID


def test_rows_without_balances_do_not_break_the_chain() -> None:
    validations = validate_balance_chain(
        (
            BalanceRow(-42_900, krw(957_100), balance_before=krw(1_000_000)),
            BalanceRow(-1_000, None),
            BalanceRow(-7_100, krw(950_000)),
        )
    )

    assert validations[0].status is BalanceStatus.VALID
    assert validations[1].status is BalanceStatus.UNAVAILABLE
    # The gap row cannot be checked, so the next row still anchors on 957,100.
    assert validations[2].status is BalanceStatus.VALID
