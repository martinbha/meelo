"""Reducing a merchant name to the thing two spellings have in common.

The same shop reaches this system under several names. A card app prints
``(주)스타벅스코리아 강남점``, the bank prints ``스타벅스강남``, and OCR turns one
of them into ``스타벅스 강남 점``. They are one merchant, and a category rule the
user wrote once has to fire on all three.

Normalization is what makes that possible, and it is deliberately lossy: it
strips the company form, the branch suffix, the payment-network noise, the
punctuation, and the spacing, because none of those distinguish one merchant
from another. What it must never do is *replace* the name the user sees. The
raw text is stored separately and shown unchanged (specification 6.7, 6.11, 18)
— a normalized name is a lookup key, not a label.

Matching then happens in two tiers:

- **Exact**, on an HMAC blind index of the normalized form, so an alias is found
  without decrypting every merchant in the database.
- **Fuzzy**, in application memory only, over names the caller has already
  decrypted for its own reasons. A similarity score cannot be computed in the
  database without giving the database the plaintext, which is the thing this
  system exists not to do.

Changing the rules below changes every blind index derived from them. Stored
indexes do not update themselves, so a change here needs a reindex (#168).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from rapidfuzz.fuzz import ratio

from apps.core.blind_index import SearchKey, blind_index
from apps.core.errors import InvalidRequestError

#: Company forms that say how a business is incorporated, not which one it is.
#: Matched as whole tokens, never as substrings: a shop called "Baltdrop" is not
#: a limited company, and treating it as one would leave "ba rop" as its key.
COMPANY_FORMS: frozenset[str] = frozenset(
    {
        "주식회사",
        "유한회사",
        "합자회사",
        "co",
        "coltd",
        "ltd",
        "inc",
        "llc",
        "corp",
        "corporation",
    }
)

#: Korean company forms written flush against the name, with no space to split
#: on. Stripped from the ends only, so one inside a name survives.
GLUED_COMPANY_FORMS: tuple[str, ...] = ("주식회사", "유한회사", "합자회사")

#: Symbols that mark a company form on their own. Removed before punctuation
#: stripping, which would otherwise leave a bare "주" behind.
COMPANY_SYMBOLS: tuple[str, ...] = ("㈜", "(주)", "（주）")

#: Words card and bank apps put on the statement line beside the merchant.
PAYMENT_TOKENS: frozenset[str] = frozenset(
    {
        "체크카드",
        "신용카드",
        "카드결제",
        "일시불",
        "할부",
        "승인",
        "결제",
    }
)

#: Branch markers. A branch is a place, not a merchant: two Starbucks branches
#: belong to the same category, and a rule written at one should fire at both.
BRANCH_SUFFIX = re.compile(r"(지점|점포|점|본점|영업소)$")

#: Everything that is punctuation or a symbol, once the string is decomposed.
_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_DIGIT_RUN = re.compile(r"\d{4,}")


def _strip_glued_forms(value: str) -> str:
    """Remove a company form written flush against the name, at either end."""

    for form in GLUED_COMPANY_FORMS:
        if value.startswith(form) and len(value) > len(form):
            value = value[len(form) :]
        if value.endswith(form) and len(value) > len(form):
            value = value[: -len(form)]
    return value


def normalize_merchant(value: str) -> str:
    """Reduce a merchant name to its comparable form.

    Raises when nothing survives: an empty key would collide with every other
    empty key, quietly filing unrelated merchants together.
    """

    # NFKC first, so a full-width or compatibility form of a character becomes
    # the same character its plain spelling would produce.
    folded = unicodedata.normalize("NFKC", value).casefold()
    for symbol in COMPANY_SYMBOLS:
        # Before punctuation stripping, which would leave a bare "주" behind.
        folded = folded.replace(symbol, " ")
    folded = _PUNCTUATION.sub(" ", folded)

    # Token by token, never by substring. Dropping "ltd" wherever it appeared
    # would turn a shop called "Baltdrop" into "ba rop".
    kept = [
        token
        for token in folded.split()
        if token not in COMPANY_FORMS
        and token not in PAYMENT_TOKENS
        # Approval and terminal numbers ride along with the name and differ per
        # transaction, so they would defeat every lookup.
        and not _DIGIT_RUN.fullmatch(token)
    ]
    joined = _strip_glued_forms("".join(kept))
    # The branch marker goes last, once spacing can no longer hide it, so
    # "강남 점" and "강남점" end the same way.
    normalized = BRANCH_SUFFIX.sub("", joined) or joined
    if not normalized:
        raise InvalidRequestError("Merchant text cannot be empty.")
    return normalized


def display_merchant(value: str) -> str:
    """The name to show a person: their own, with only the spacing tidied.

    Normalization is for lookups. Showing its output would tell the user their
    coffee came from ``스타벅스강남`` when the receipt says something else.
    """

    return " ".join(unicodedata.normalize("NFKC", value).split())


def merchant_blind_index(value: str, *, user_id: Any, key: SearchKey | bytes) -> str:
    """A searchable token for one merchant, revealing nothing about the name.

    Built from the *normalized* form, which is what lets three spellings of one
    shop share a category rule. The keying, domain separation, and versioning
    come from :mod:`apps.core.blind_index`; this function only decides what the
    value is.
    """

    return blind_index("merchant", normalize_merchant(value), user_id=user_id, key=key)


#: Similarity at or above which two names are treated as the same merchant.
SIMILARITY_THRESHOLD = 88.0
#: Similarity below which a pair is not worth showing a reviewer at all.
REVIEW_THRESHOLD = 70.0


@dataclass(frozen=True, slots=True)
class MerchantSimilarity:
    """How alike two merchant names are, and what that justifies."""

    candidate: str
    normalized: str
    score: float

    @property
    def is_confident(self) -> bool:
        """Strong enough to treat as the same merchant without asking."""

        return self.score >= SIMILARITY_THRESHOLD

    @property
    def needs_review(self) -> bool:
        """Plausible, but a person decides. Never hidden."""

        return REVIEW_THRESHOLD <= self.score < SIMILARITY_THRESHOLD


def similarity(left: str, right: str) -> float:
    """Compare two merchant names on their normalized forms."""

    try:
        return float(ratio(normalize_merchant(left), normalize_merchant(right)))
    except InvalidRequestError:
        return 0.0


def rank_candidates(merchant: str, candidates: Iterable[str]) -> tuple[MerchantSimilarity, ...]:
    """Score decrypted candidate names against one merchant, best first.

    In memory by design: scoring in the database would mean putting merchant
    plaintext there. Callers pass names they have already decrypted for their
    own reasons, so this adds no decryption of its own.

    Everything at or above :data:`REVIEW_THRESHOLD` is returned, including the
    uncertain ones — a weak match that is hidden cannot be corrected.
    """

    ranked = [
        MerchantSimilarity(
            candidate, normalize_merchant(candidate), similarity(merchant, candidate)
        )
        for candidate in candidates
        if candidate.strip()
    ]
    return tuple(
        sorted(
            (item for item in ranked if item.score >= REVIEW_THRESHOLD),
            key=lambda item: (-item.score, item.normalized),
        )
    )


def best_candidate(merchant: str, candidates: Iterable[str]) -> MerchantSimilarity | None:
    """The single strongest candidate, or nothing when none is worth showing."""

    ranked = rank_candidates(merchant, candidates)
    return ranked[0] if ranked else None
