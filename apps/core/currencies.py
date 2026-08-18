"""Which currencies exist here, and how many decimal places each one has.

A currency code is not three arbitrary letters. Each one carries a minor-unit
exponent, and getting it wrong is not a display bug: ``42900`` minor units is
₩42,900 in Korea and $429.00 in the United States. A system that stores minor
units — which this one does, because binary floating point has no business
anywhere near money (specification 15.1) — has to know the exponent to turn a
stored integer back into a figure a person recognises.

So the exponent lives in one table, and everything that parses, stores, or
renders money reads it from there. Two consequences follow:

**Unknown codes are refused at the boundary.** A three-letter string that
happens to look like a currency is not one. Accepting ``"XYZ"`` and defaulting to
two decimals would store amounts scaled by 100 for a currency nobody supports,
and the mistake would only become visible as figures a hundred times too small.
Refusing it where it enters means the OCR output that produced it gets reviewed
instead.

**Currencies are never converted.** There are no exchange rates here, so two
currencies cannot be added, subtracted, or compared. That refusal lives in
:class:`~apps.core.value_objects.Money`; the registry only says which codes are
real and what each one's exponent is.

Adding a currency is adding a row. The symbol is what the parsers look for on a
screenshot and what rendering puts in front of the figure.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


class UnknownCurrencyError(ValueError):
    """This code is not a currency the application supports."""


@dataclass(frozen=True, slots=True)
class CurrencyDefinition:
    """One currency: what to call it, what to print, and how to scale it."""

    code: str
    name: str
    symbol: str
    #: How many decimal places the currency has. ``0`` for KRW and JPY, ``2``
    #: for most others, ``3`` for the Gulf dinars.
    minor_unit_exponent: int
    #: Whether the symbol is written before the digits. ``₩42,900`` and
    #: ``$10.25`` lead; a currency written ``10,25 €`` would not.
    symbol_leads: bool = True

    @property
    def minor_units_per_unit(self) -> int:
        return 10**self.minor_unit_exponent


_DEFINITIONS: tuple[CurrencyDefinition, ...] = (
    CurrencyDefinition("KRW", "South Korean won", "₩", 0),
    CurrencyDefinition("JPY", "Japanese yen", "¥", 0),
    CurrencyDefinition("USD", "United States dollar", "$", 2),
    CurrencyDefinition("EUR", "Euro", "€", 2),
    CurrencyDefinition("GBP", "Pound sterling", "£", 2),
    CurrencyDefinition("CNY", "Chinese yuan", "¥", 2),
    CurrencyDefinition("HKD", "Hong Kong dollar", "HK$", 2),
    CurrencyDefinition("SGD", "Singapore dollar", "S$", 2),
    CurrencyDefinition("AUD", "Australian dollar", "A$", 2),
    CurrencyDefinition("CAD", "Canadian dollar", "C$", 2),
    CurrencyDefinition("CHF", "Swiss franc", "CHF", 2),
    CurrencyDefinition("THB", "Thai baht", "฿", 2),
    CurrencyDefinition("VND", "Vietnamese dong", "₫", 0),
    CurrencyDefinition("TWD", "New Taiwan dollar", "NT$", 2),
)

REGISTRY: Mapping[str, CurrencyDefinition] = MappingProxyType(
    {definition.code: definition for definition in _DEFINITIONS}
)

#: The currency a screenshot is assumed to be in when nothing on it says
#: otherwise. This is a Korean personal-finance system; a bank list with no
#: symbol on it is in won.
DEFAULT_CURRENCY_CODE = "KRW"


def normalize_code(code: str) -> str:
    """Upper-case and strip a code without deciding whether it is real."""

    return (code or "").strip().upper()


def is_supported(code: str) -> bool:
    return normalize_code(code) in REGISTRY


def definition_for(code: str) -> CurrencyDefinition:
    """The registry row for one code, or a refusal naming the code."""

    normalized = normalize_code(code)
    try:
        return REGISTRY[normalized]
    except KeyError as exc:
        raise UnknownCurrencyError(
            f"'{normalized or code}' is not a currency this application supports."
        ) from exc


def minor_unit_exponent(code: str) -> int:
    return definition_for(code).minor_unit_exponent


def symbol_for(code: str) -> str:
    return definition_for(code).symbol


def supported_codes() -> tuple[str, ...]:
    return tuple(sorted(REGISTRY))


def currency_choices() -> tuple[tuple[str, str], ...]:
    """``(code, label)`` pairs for a form, ordered by code."""

    return tuple((code, f"{REGISTRY[code].name} ({code})") for code in supported_codes())


def currency_markers() -> tuple[tuple[str, str], ...]:
    """Match keys a screenshot may print, mapped to their code.

    Every key is casefolded, because these are compared against casefolded OCR
    text: a symbol such as ``HK$`` carries letters, and left as written it would
    never match the ``hk$`` the comparison actually sees — leaving ``$`` to win
    and filing a Hong Kong dollar amount as US dollars.

    Ordered longest first for the same reason in the other direction: ``$`` is a
    prefix of ``HK$``, and a shorter marker that matches first would take the
    amount before the specific one is tried.

    Derived from the registry rather than written out again, so a currency added
    to the registry cannot be one the parsers are unable to see.
    """

    markers: list[tuple[str, str]] = []
    for definition in REGISTRY.values():
        markers.append((definition.symbol.casefold(), definition.code))
        markers.append((definition.code.casefold(), definition.code))
    return tuple(sorted(set(markers), key=lambda item: (-len(item[0]), item[0])))
