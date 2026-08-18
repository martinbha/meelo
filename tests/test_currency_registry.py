"""Minor units are not a display detail (#156, specification 15.1).

``42900`` is ₩42,900 in Korea and $429.00 in the United States. The integer is
the same; the money is not. Everything here follows from that: the exponent is
looked up rather than assumed, a code with no entry in the registry has no
defensible exponent and is refused, and two currencies are never added because
there is no exchange rate anywhere in this application to add them with.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.core.currencies import (
    DEFAULT_CURRENCY_CODE,
    REGISTRY,
    UnknownCurrencyError,
    currency_choices,
    currency_markers,
    definition_for,
    is_supported,
    minor_unit_exponent,
    supported_codes,
)
from apps.core.value_objects import (
    Currency,
    CurrencyMismatchError,
    InvalidCurrencyError,
    Money,
)
from apps.observations.models import ImportedObservation
from apps.parsing.money import CURRENCY_MARKERS, parse_money
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_document, make_ocr_run, make_user

# ----------------------------------------------------------------------
# The registry itself
# ----------------------------------------------------------------------


def test_zero_decimal_and_two_decimal_currencies_are_both_described() -> None:
    assert minor_unit_exponent("KRW") == 0
    assert minor_unit_exponent("JPY") == 0
    assert minor_unit_exponent("USD") == 2
    assert definition_for("KRW").minor_units_per_unit == 1
    assert definition_for("USD").minor_units_per_unit == 100


def test_every_definition_is_internally_consistent() -> None:
    for code, definition in REGISTRY.items():
        assert code == definition.code == code.upper()
        assert len(code) == 3
        assert definition.name and definition.symbol
        assert 0 <= definition.minor_unit_exponent <= 3


def test_an_unknown_code_is_refused_by_name() -> None:
    with pytest.raises(UnknownCurrencyError, match="XYZ"):
        definition_for("xyz")
    assert not is_supported("XYZ")
    assert is_supported(" krw ")


def test_the_default_currency_is_in_the_registry() -> None:
    assert DEFAULT_CURRENCY_CODE in supported_codes()


def test_choices_and_markers_are_derived_rather_than_repeated() -> None:
    codes = {code for code, _ in currency_choices()}
    assert codes == set(supported_codes())

    markers = dict(currency_markers())
    for code, definition in REGISTRY.items():
        # ¥ is shared by JPY and CNY, so only one of them can own that key.
        assert markers[definition.symbol.casefold()] == code or definition.symbol == "¥"
        assert markers[code.casefold()] == code
    # Longest first, so a marker that is a prefix of another cannot win.
    lengths = [len(marker) for marker, _ in currency_markers()]
    assert lengths == sorted(lengths, reverse=True)


# ----------------------------------------------------------------------
# Parsing and rendering
# ----------------------------------------------------------------------


def test_krw_parses_and_renders_with_no_decimals() -> None:
    candidate = parse_money("42,900원")

    assert candidate is not None
    assert candidate.money == Money(42_900, "KRW")
    assert candidate.money.format() == "₩42,900"
    assert str(candidate.money) == "42900 KRW"


def test_usd_parses_and_renders_with_two() -> None:
    candidate = parse_money("$10.25")

    assert candidate is not None
    assert candidate.money == Money(1_025, "USD")
    assert candidate.money.format() == "$10.25"
    assert candidate.money.decimal_amount == Decimal("10.25")


def test_the_same_integer_is_different_money_in_different_currencies() -> None:
    """The whole reason the exponent cannot be assumed."""

    assert Money(42_900, "KRW").format() == "₩42,900"
    assert Money(42_900, "USD").format() == "$429.00"


def test_a_negative_amount_keeps_its_sign_in_front_of_the_symbol() -> None:
    assert Money(-1_025, "USD").format() == "-$10.25"
    assert Money(-1_025, "USD").format(symbol=False) == "-10.25 USD"


def test_a_screenshot_currency_marker_wins_over_a_shorter_prefix() -> None:
    """``HK$`` must not be read as ``$``."""

    longest = CURRENCY_MARKERS[0][0]
    assert len(longest) >= len("HK$")
    hong_kong = parse_money("HK$120.00")
    assert hong_kong is not None
    assert hong_kong.money == Money(12_000, "HKD")


# ----------------------------------------------------------------------
# Mixed-currency arithmetic
# ----------------------------------------------------------------------


def test_adding_two_currencies_raises_instead_of_producing_a_total() -> None:
    with pytest.raises(CurrencyMismatchError):
        Money(1_000, "KRW") + Money(1_000, "USD")
    with pytest.raises(CurrencyMismatchError):
        Money(1_000, "KRW") - Money(1_000, "USD")


def test_two_currencies_are_never_equal_even_at_the_same_number() -> None:
    assert Money(1_000, "KRW") != Money(1_000, "USD")


# ----------------------------------------------------------------------
# The boundary
# ----------------------------------------------------------------------


def test_the_value_object_refuses_an_unsupported_code() -> None:
    with pytest.raises(InvalidCurrencyError, match="XYZ"):
        Currency("XYZ")
    with pytest.raises(InvalidCurrencyError):
        Currency("US")
    with pytest.raises(InvalidCurrencyError):
        Money(100, "XYZ")


@pytest.mark.django_db
def test_a_financial_account_refuses_an_unsupported_code() -> None:
    user = make_user(email="currency-account@example.com")
    account = make_account(user, currency="krw", opening_balance_encrypted="0:KRW")
    account.full_clean()
    assert account.currency == "KRW"

    account.currency = "XYZ"
    with pytest.raises(ValidationError, match="XYZ"):
        account.full_clean()


@pytest.mark.django_db
def test_a_transaction_refuses_an_unsupported_code() -> None:
    user = make_user(email="currency-transaction@example.com")
    account = make_account(user)
    transaction = CanonicalTransaction(
        user=user,
        created_by=user,
        financial_account=account,
        occurred_at=date(2026, 8, 7),
        amount_encrypted="100:KRW",
        currency="XYZ",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    with pytest.raises(ValidationError, match="XYZ"):
        transaction.full_clean()


@pytest.mark.django_db
def test_an_observation_refuses_an_unsupported_code() -> None:
    user = make_user(email="currency-observation@example.com")
    document = make_document(user)
    run = make_ocr_run(user, document)
    observation = ImportedObservation(
        user=user,
        source_document=document,
        ocr_run=run,
        currency="XYZ",
    )

    with pytest.raises(ValidationError, match="XYZ"):
        observation.full_clean()


def test_a_blank_observation_currency_is_still_allowed() -> None:
    """A row whose amount never parsed has no currency, and that is not an error."""

    observation = ImportedObservation(currency="")
    assert observation.currency == ""
