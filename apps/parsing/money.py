"""Extract amounts and currencies from Korean and English banking text.

Amounts arrive as raw OCR text (``42,900원``, ``-1,200 KRW``, ``$10.25``) or as
text already normalized by :mod:`apps.ocr.normalization`. Values are stored as
integer minor units so no binary floating point ever touches money.

The sign and label printed on the screen are preserved verbatim. Turning them
into an economic direction is the job of :mod:`apps.parsing.direction`, because
the same ``결제`` label means opposite things on a card list and on a
credit-card payment receipt.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from apps.core.value_objects import Currency, InvalidMoneyError, Money

#: Currency markers that may appear before or after the digits.
CURRENCY_MARKERS: tuple[tuple[str, str], ...] = (
    ("₩", "KRW"),
    ("원", "KRW"),
    ("krw", "KRW"),
    ("$", "USD"),
    ("usd", "USD"),
    ("€", "EUR"),
    ("eur", "EUR"),
    ("¥", "JPY"),
    ("jpy", "JPY"),
)

#: Labels printed next to an amount. Recorded as-is; direction is decided later.
AMOUNT_LABELS: tuple[str, ...] = (
    "입금",
    "출금",
    "결제",
    "승인",
    "취소",
    "환불",
    "이체",
    "송금",
    "납부",
    "청구",
    "잔액",
    "deposit",
    "withdrawal",
    "payment",
    "approved",
    "approval",
    "refund",
    "balance",
    "transfer",
)

#: Glyphs OCR routinely confuses with digits, repaired only inside amounts.
DIGIT_REPAIRS = {
    "O": "0",
    "o": "0",
    "D": "0",
    "Q": "0",
    "l": "1",
    "I": "1",
    "|": "1",
    "S": "5",
    "s": "5",
    "B": "8",
    "Z": "2",
    "b": "6",
    "g": "9",
}

DIGITS_RE = re.compile(r"\d")
SIGN_RE = re.compile(r"[+\-−–—]")
GROUPED_RE = re.compile(r"^\d{1,3}(?:([.,])\d{3})+$")
NUMERIC_BODY_RE = re.compile(r"\d[\d.,]*")
MARKER_RE = re.compile(
    "|".join(re.escape(marker) for marker, _ in CURRENCY_MARKERS),
    re.IGNORECASE,
)

CONFIDENCE_CLEAN = 0.98
CONFIDENCE_REPAIRED = 0.7
CONFIDENCE_AMBIGUOUS = 0.3


class AmbiguousAmountError(ValueError):
    """An amount candidate has more than one defensible reading."""


@dataclass(frozen=True, slots=True)
class MoneyCandidate:
    """One amount read from a screenshot, with its provenance intact."""

    source_text: str
    money: Money | None
    source_sign: str
    source_label: str | None
    confidence: float
    ambiguous: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def requires_review(self) -> bool:
        return self.money is None or self.ambiguous

    @property
    def signed_minor(self) -> int | None:
        """Minor units carrying the sign printed on screen, if any."""

        if self.money is None:
            return None
        return -self.money.amount_minor if self.source_sign == "-" else self.money.amount_minor


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _detect_currency(text: str) -> str | None:
    lowered = text.casefold()
    for marker, code in CURRENCY_MARKERS:
        if marker in lowered:
            return code
    return None


def _detect_label(text: str) -> str | None:
    lowered = text.casefold()
    for label in AMOUNT_LABELS:
        if label in lowered:
            return label
    return None


def _residual(text: str, label: str | None) -> str:
    """Strip currency markers and labels, leaving the signed numeric body."""

    stripped = MARKER_RE.sub(" ", text)
    if label is not None:
        stripped = re.sub(re.escape(label), " ", stripped, flags=re.IGNORECASE)
    return stripped.strip()


def _split_sign(residual: str) -> tuple[str, str]:
    """Split a leading sign from the rest of the body.

    Only a leading sign counts. A hyphen inside the digits belongs to
    something that is not an amount — an ISO date, an account number — and
    leaving it in place lets the numeric-body check reject the token.
    """

    if not residual:
        return "", residual
    if SIGN_RE.fullmatch(residual[0]):
        return ("+" if residual[0] == "+" else "-"), residual[1:].strip()
    return "", residual


def _repair_digits(raw: str) -> tuple[str, bool]:
    """Repair glyphs OCR confuses with digits, but only inside a numeric body.

    The repair is discarded unless it makes the whole residual numeric, so
    merchant text is never rewritten into an amount.
    """

    if not any(character.isdigit() for character in raw):
        return raw, False
    repaired = "".join(DIGIT_REPAIRS.get(character, character) for character in raw)
    if repaired == raw:
        return raw, False
    if not NUMERIC_BODY_RE.fullmatch(repaired.replace(" ", "")):
        return raw, False
    return repaired, True


def _interpret_separators(digits: str, currency: Currency) -> tuple[Decimal, tuple[str, ...]]:
    """Turn a grouped numeric string into a Decimal, or reject it as ambiguous.

    Raises :class:`AmbiguousAmountError` when the separators support more than
    one reading, so an amount is never silently misread by a factor of a
    thousand.
    """

    compact = digits.replace(" ", "")
    if not compact:
        raise AmbiguousAmountError("no digits present")
    if compact.isdigit():
        return Decimal(compact), ()

    separators = [character for character in compact if character in ".,"]
    distinct = set(separators)

    if len(distinct) == 2:
        # Both separators present: the rightmost one is the decimal point.
        decimal_separator = compact[max(compact.rfind("."), compact.rfind(","))]
        grouping = "." if decimal_separator == "," else ","
        integer, _, fraction = compact.rpartition(decimal_separator)
        if (
            not re.fullmatch(rf"\d{{1,3}}(?:\{grouping}\d{{3}})+", integer)
            or not fraction.isdigit()
        ):
            raise AmbiguousAmountError(f"unreadable separator layout in '{digits}'")
        if currency.decimal_places == 0:
            raise AmbiguousAmountError(
                f"'{digits}' shows a fraction but {currency} has no minor units"
            )
        return Decimal(f"{integer.replace(grouping, '')}.{fraction}"), ("mixed_separators",)

    separator = separators[0]
    if GROUPED_RE.fullmatch(compact):
        # Uniform groups of three. For a zero-decimal currency this can only be
        # grouping; otherwise a single group is genuinely ambiguous.
        if currency.decimal_places == 0 or compact.count(separator) > 1:
            return Decimal(compact.replace(separator, "")), ("thousands_grouping",)
        raise AmbiguousAmountError(
            f"'{digits}' reads as either grouping or a fraction in {currency}"
        )

    head, _, tail = compact.rpartition(separator)
    if separator in compact[: compact.rfind(separator)] or not head.isdigit() or not tail.isdigit():
        raise AmbiguousAmountError(f"unreadable separator layout in '{digits}'")
    if currency.decimal_places == 0:
        raise AmbiguousAmountError(f"'{digits}' shows a fraction but {currency} has no minor units")
    if len(tail) != currency.decimal_places:
        raise AmbiguousAmountError(
            f"'{digits}' has {len(tail)} fractional digits but {currency} expects "
            f"{currency.decimal_places}"
        )
    return Decimal(f"{head}.{tail}"), ("decimal_fraction",)


def parse_money(text: str, *, default_currency: str | Currency = "KRW") -> MoneyCandidate | None:
    """Parse one amount token.

    Returns ``None`` when the text is not an amount at all, and a candidate with
    ``money=None`` and ``ambiguous=True`` when it looks like an amount but
    cannot be read unambiguously.
    """

    cleaned = _clean(text)
    if not cleaned or DIGITS_RE.search(cleaned) is None:
        return None

    label = _detect_label(cleaned)
    detected_currency = _detect_currency(cleaned)
    currency = Currency(detected_currency) if detected_currency else Currency(str(default_currency))

    sign, unsigned = _split_sign(_residual(cleaned, label))
    repaired, was_repaired = _repair_digits(unsigned)
    body = repaired.replace(" ", "")
    if not NUMERIC_BODY_RE.fullmatch(body):
        # Leftover text means this token carries more than an amount.
        return None

    reasons: list[str] = []
    if detected_currency is None:
        reasons.append("currency_defaulted")
    if was_repaired:
        reasons.append("ocr_digit_repair")

    try:
        value, separator_reasons = _interpret_separators(body, currency)
    except AmbiguousAmountError as exc:
        return MoneyCandidate(
            source_text=cleaned,
            money=None,
            source_sign=sign,
            source_label=label,
            confidence=CONFIDENCE_AMBIGUOUS,
            ambiguous=True,
            reasons=(*reasons, f"ambiguous: {exc}"),
        )
    reasons.extend(separator_reasons)

    try:
        money = Money.from_decimal(value, currency)
    except InvalidMoneyError as exc:  # pragma: no cover - guarded by the regex
        return MoneyCandidate(
            source_text=cleaned,
            money=None,
            source_sign=sign,
            source_label=label,
            confidence=CONFIDENCE_AMBIGUOUS,
            ambiguous=True,
            reasons=(*reasons, f"invalid: {exc}"),
        )

    confidence = CONFIDENCE_REPAIRED if was_repaired else CONFIDENCE_CLEAN
    return MoneyCandidate(
        source_text=cleaned,
        money=money,
        source_sign=sign,
        source_label=label,
        confidence=confidence,
        reasons=tuple(reasons),
    )


def looks_like_money(text: str) -> bool:
    """Report whether a token is worth handing to :func:`parse_money`.

    Bare digit runs are excluded: an account suffix or a row index is not an
    amount unless a currency marker, a sign, a separator, or a money label says
    so.
    """

    cleaned = _clean(text)
    if not cleaned or DIGITS_RE.search(cleaned) is None:
        return False
    if _detect_currency(cleaned) is not None or _detect_label(cleaned) is not None:
        return True
    return bool(re.search(r"\d[.,]\d", cleaned)) or bool(SIGN_RE.match(cleaned))


def decimal_amount(candidate: MoneyCandidate) -> Decimal | None:
    """The candidate's major-unit value, for display and comparison only."""

    try:
        return candidate.money.decimal_amount if candidate.money is not None else None
    except InvalidOperation:  # pragma: no cover - Money guarantees validity
        return None
