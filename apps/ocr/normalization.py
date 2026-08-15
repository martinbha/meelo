from __future__ import annotations

import re
import unicodedata

WHITESPACE_RE = re.compile(r"\s+")
MONEY_RE = re.compile(
    r"^(?P<sign>[+-]?)\s*(?:₩\s*)?(?P<amount>\d[\d,\s]*)\s*(?:원|krw)?$",
    re.IGNORECASE,
)
KOREAN_DATE_RE = re.compile(r"^(\d{2,4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일?$")
DELIMITED_DATE_RE = re.compile(r"^(\d{2,4})\s*[./]\s*(\d{1,2})\s*[./]\s*(\d{1,2})$")


def _date(year: str, month: str, day: str) -> str:
    return f"{year.zfill(4)}-{month.zfill(2)}-{day.zfill(2)}"


def normalize_money_text(value: str, *, locale: str = "ko-KR") -> str | None:
    normalized = WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    lowered = normalized.casefold()
    has_money_syntax = any(marker in lowered for marker in ("₩", "원", "krw", ","))
    has_grouped_space = bool(re.search(r"\d\s+\d", normalized))
    if not has_money_syntax and not has_grouped_space:
        return None
    match = MONEY_RE.fullmatch(normalized)
    if match is None:
        return None
    amount = re.sub(r"[,\s]", "", match.group("amount"))
    if not amount.isdigit():
        return None
    currency = "KRW" if locale.casefold() in {"ko", "ko-kr"} else "KRW"
    sign = "-" if match.group("sign") == "-" else ""
    return f"{sign}{int(amount)} {currency}"


def normalize_ocr_text(value: str, *, locale: str = "ko-KR") -> str:
    normalized = WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    money = normalize_money_text(normalized, locale=locale)
    if money is not None:
        return money
    korean_date = KOREAN_DATE_RE.fullmatch(normalized)
    if korean_date is not None:
        return _date(*korean_date.groups())
    delimited_date = DELIMITED_DATE_RE.fullmatch(normalized)
    if delimited_date is not None:
        return _date(*delimited_date.groups())
    normalized = re.sub(r"\s*([:;,])\s*", r"\1 ", normalized)
    return WHITESPACE_RE.sub(" ", normalized).strip().casefold()
