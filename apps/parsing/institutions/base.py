"""Shared machinery for institution-specific screenshot parsers.

Every Korean banking and card app draws the same handful of things — a date, a
counterparty, an amount, sometimes a running balance — with different chrome
around them. The differences live in :class:`InstitutionProfile`; the row
reading, field extraction, balance chaining, and error containment live here.

Two safety rules shape the design:

* A parser that does not recognise the layout falls back to the generic parser
  rather than inventing rows.
* A row that raises is reported as an unreadable observation, never as a worker
  crash, so one odd screenshot cannot stall processing.
"""

from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

from ..balances import BalanceRow, BalanceStatus, BalanceValidation, validate_balance_chain
from ..contracts import (
    DocumentMetadata,
    NormalizedToken,
    ParsedCardPayment,
    ParsedObservation,
    ParsedStatement,
    ParserMetadata,
    ParserSupport,
    ScreenshotParser,
    TransactionDirection,
)
from ..dates import (
    DateContext,
    ResolvedDate,
    looks_like_date,
    resolve_date,
    resolve_explicit_date,
)
from ..direction import DirectionResolution, is_direction_label, resolve_direction
from ..generic import GenericTransactionListParser
from ..money import MoneyCandidate, looks_like_money, parse_money
from ..rows import average_confidence, group_rows, row_region

#: A run of masking glyphs, which marks a redacted account or card number.
MASK_RUN_RE = re.compile(r"[*·xX•]{2,}")
DIGIT_GROUP_RE = re.compile(r"\d+")
TRAILING_SUFFIX_RE = re.compile(r"\b(\d{4})\s*$")
SUFFIX_CONTEXT = ("카드", "계좌", "card", "account", "번호")

APPROVAL_LABELS = ("승인번호", "거래번호", "approval", "authorization", "auth no")
APPROVAL_DIGITS_RE = re.compile(r"\b(\d{6,12})\b")

INSTALLMENT_RE = re.compile(r"(\d{1,2})\s*개월")
SINGLE_PAYMENT_LABELS = ("일시불", "single payment", "lump sum")
INSTALLMENT_LABELS = ("할부", "installment")

BALANCE_LABELS = ("잔액", "잔고", "balance", "남은금액")

#: Detail screens are key/value lists. These are the keys, never counterparties.
FIELD_LABELS = frozenset(
    {
        "출금계좌",
        "입금계좌",
        "받는분",
        "보내는분",
        "받는분계좌",
        "거래일시",
        "거래일자",
        "이용일자",
        "승인일시",
        "가맹점",
        "가맹점명",
        "카드번호",
        "계좌번호",
        "이용금액",
        "결제금액",
        "적요",
        "메모",
        "내용",
    }
)

#: Source types that describe one transaction spread down the screen rather
#: than a list of transactions, one per row.
SINGLE_TRANSACTION_SOURCE_TYPES = frozenset(
    {
        "bank_transaction_detail",
        "bank_transfer_confirmation",
        "card_transaction_detail",
        "credit_card_payment",
    }
)

#: Column headers that tell which side of the ledger an amount column holds.
DEBIT_COLUMN_HEADERS = frozenset({"출금", "출금액", "지급", "withdrawal", "withdrawn"})
CREDIT_COLUMN_HEADERS = frozenset({"입금", "입금액", "예입", "deposit", "deposited"})

#: Scoring for :meth:`InstitutionParser.supports`.
BASE_SUPPORT_SCORE = 0.55
MARKER_SUPPORT_STEP = 0.1
MAXIMUM_SUPPORT_SCORE = 0.95
HINT_SUPPORT_BONUS = 0.15

#: Confidence for a direction read from an amount's column rather than a label.
COLUMN_DIRECTION_CONFIDENCE = 0.8


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _folded(value: str) -> str:
    return _clean(value).casefold()


@dataclass(frozen=True, slots=True)
class InstitutionProfile:
    """Everything that distinguishes one institution's screens from another's."""

    name: str
    version: str
    display_name: str
    #: Text that identifies the app itself. At least one must appear.
    institution_markers: tuple[str, ...]
    #: Column headers and row labels that confirm a known layout.
    layout_markers: tuple[str, ...] = ()
    #: Chrome rows — tabs, buttons, headers — that carry no transaction.
    chrome_markers: tuple[str, ...] = ()
    #: Markers that identify a specific source type, most specific first.
    source_type_markers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    default_source_type: str = "bank_transaction_list"
    default_currency: str = "KRW"
    #: True when rows print a running balance in their rightmost amount column.
    balance_column: bool = True
    #: True when the newest transaction is drawn at the top, as most apps do.
    rows_newest_first: bool = True
    #: Screens that show one transaction as a vertical key/value list, whose
    #: rows must be read together rather than as separate transactions.
    single_transaction_source_types: frozenset[str] = SINGLE_TRANSACTION_SOURCE_TYPES


@dataclass(frozen=True, slots=True)
class DirectionColumns:
    """Where a statement's withdrawal and deposit columns sit horizontally.

    Korean bank statements print unsigned amounts under 출금 and 입금 headers.
    The column an amount is drawn in is the only thing that says which way the
    money moved, so the header positions are read once per screenshot.
    """

    debit_center: float | None = None
    credit_center: float | None = None

    @property
    def is_usable(self) -> bool:
        return self.debit_center is not None and self.credit_center is not None

    def classify(self, center: float) -> TransactionDirection:
        if not self.is_usable:
            return TransactionDirection.UNKNOWN
        assert self.debit_center is not None and self.credit_center is not None
        debit_distance = abs(center - self.debit_center)
        credit_distance = abs(center - self.credit_center)
        if debit_distance == credit_distance:
            return TransactionDirection.UNKNOWN
        return (
            TransactionDirection.DEBIT
            if debit_distance < credit_distance
            else TransactionDirection.CREDIT
        )


@dataclass(frozen=True, slots=True)
class RowFields:
    """The raw evidence one visual row contributes."""

    row: tuple[NormalizedToken, ...]
    resolved_date: ResolvedDate
    amount: MoneyCandidate | None
    balance: MoneyCandidate | None
    extra_amounts: tuple[MoneyCandidate, ...]
    labels: tuple[str, ...]
    merchant: str | None
    instrument_suffix: str | None
    approval_code: str | None
    installment_months: int | None
    #: Horizontal centre of the amount token, used for column classification.
    amount_center: float | None = None

    @property
    def has_transaction_signal(self) -> bool:
        """Whether the row looks like a transaction rather than chrome."""

        return self.amount is not None or self.resolved_date.value is not None


class InstitutionParser(ScreenshotParser):
    """A profile-driven parser for one institution's screenshots."""

    profile: InstitutionProfile

    def __init__(self, profile: InstitutionProfile | None = None) -> None:
        if profile is not None:
            self.profile = profile
        if not getattr(self, "profile", None):
            raise ValueError("An institution parser requires a profile.")
        self._fallback = GenericTransactionListParser()

    # ------------------------------------------------------------------
    # Contract
    # ------------------------------------------------------------------

    @property
    def metadata(self) -> ParserMetadata:
        return ParserMetadata(self.profile.name, self.profile.version)

    def supports(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> ParserSupport:
        haystack = " ".join(_folded(token.text) for token in tokens)
        institution_hits = tuple(
            marker for marker in self.profile.institution_markers if _folded(marker) in haystack
        )
        hint = _folded(document.institution_hint or "")
        hint_matches = bool(hint) and any(
            _folded(marker) in hint or hint in _folded(marker)
            for marker in (self.profile.name, self.profile.display_name)
            + self.profile.institution_markers
        )
        if not institution_hits and not hint_matches:
            return ParserSupport(0.0, self.detect_source_type(document, haystack), ())

        layout_hits = tuple(
            marker for marker in self.profile.layout_markers if _folded(marker) in haystack
        )
        # A recognised screen title is as much evidence of a known layout as a
        # column header is.
        screen_hits = tuple(
            marker
            for markers in self.profile.source_type_markers.values()
            for marker in markers
            if _folded(marker) in haystack
        )
        score = BASE_SUPPORT_SCORE + MARKER_SUPPORT_STEP * (
            max(len(institution_hits) - 1, 0) + len(layout_hits) + len(screen_hits)
        )
        if hint_matches:
            score += HINT_SUPPORT_BONUS
        reasons = tuple(
            f"matched {marker}" for marker in (*institution_hits, *layout_hits, *screen_hits)
        ) or ("matched institution hint",)
        return ParserSupport(
            min(MAXIMUM_SUPPORT_SCORE, score),
            self.detect_source_type(document, haystack),
            reasons,
        )

    def parse(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> tuple[ParsedObservation, ...]:
        grouped = group_rows(tokens)
        columns = self.detect_direction_columns(grouped)
        haystack = " ".join(_folded(token.text) for token in tokens)
        # Resolved once for the whole screenshot: a screen title usually sits
        # on the header row, not on the row whose direction depends on it.
        source_type = self.detect_source_type(document, haystack)
        context = self.date_context(document, tokens)
        candidate_rows = [row for row in grouped if not self.is_chrome_row(row)]
        if source_type in self.profile.single_transaction_source_types:
            candidate_rows = self.merge_rows(candidate_rows)

        fields: list[RowFields] = []
        failures: list[tuple[tuple[NormalizedToken, ...], str]] = []
        header_suffix: str | None = None
        for row in candidate_rows:
            try:
                extracted = self.extract_row(row, document, context)
            except Exception as error:  # noqa: BLE001 - one bad row must not stop the page
                failures.append((row, f"{type(error).__name__}: {error}"))
                continue
            if extracted.has_transaction_signal:
                fields.append(extracted)
            elif header_suffix is None and extracted.instrument_suffix is not None:
                # The account or card header names the instrument every row
                # below it belongs to.
                header_suffix = extracted.instrument_suffix

        if not fields:
            if failures:
                # Rows that failed outright stay visible to review rather than
                # being replaced by a fallback guess.
                return tuple(self.unreadable_observation(row, reason) for row, reason in failures)
            # The institution was recognised but no row looked like a
            # transaction. Fall back rather than reporting an empty screenshot.
            return tuple(
                replace(
                    observation,
                    confidence_factors={
                        **observation.confidence_factors,
                        "institution_fallback": self.profile.name,
                        "requires_review": True,
                    },
                )
                for observation in self._fallback.parse(document, tokens)
            )

        directions = [
            self.resolve_row_direction(document, item, columns, source_type=source_type)
            for item in fields
        ]
        validations = self.validate_balances(fields, directions)
        observations = [
            self.build_observation(
                document,
                item,
                direction,
                validation,
                header_suffix,
                source_type=source_type,
            )
            for item, direction, validation in zip(fields, directions, validations, strict=True)
        ]
        observations.extend(self.unreadable_observation(row, reason) for row, reason in failures)
        return tuple(observations)

    def build_statement(
        self,
        document: DocumentMetadata,
        observations: Sequence[ParsedObservation],
    ) -> ParsedStatement | None:
        """Separate one statement total from its purchase line items."""
        totals = [item for item in observations if item.is_settlement]
        if len(totals) != 1:
            return None
        summary = totals[0]
        if (
            document.statement_month is None
            or summary.occurred_on is None
            or summary.amount_minor is None
            or summary.currency is None
        ):
            return None
        period_start = document.statement_month.replace(day=1)
        period_end = period_start.replace(day=monthrange(period_start.year, period_start.month)[1])
        line_items = tuple(item for item in observations if item is not summary)
        return ParsedStatement(
            period_start=period_start,
            period_end=period_end,
            due_date=summary.occurred_on,
            total_minor=summary.amount_minor,
            currency=summary.currency,
            summary=summary,
            line_items=line_items,
        )

    def build_card_payment(
        self, observations: Sequence[ParsedObservation]
    ) -> ParsedCardPayment | None:
        """Build one issuer-qualified settlement without guessing ambiguity."""
        candidates = [item for item in observations if item.is_settlement]
        if len(candidates) != 1:
            return None
        summary = candidates[0]
        if (
            summary.occurred_on is None
            or summary.amount_minor is None
            or summary.currency is None
            or summary.ambiguous_fields
        ):
            return None
        return ParsedCardPayment(
            issuer=self.profile.name,
            occurred_on=summary.occurred_on,
            amount_minor=summary.amount_minor,
            currency=summary.currency,
            instrument_suffix=summary.instrument_suffix,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def detect_source_type(self, document: DocumentMetadata, haystack: str) -> str:
        """Infer what kind of screen this is from its markers."""

        for source_type, markers in self.profile.source_type_markers.items():
            if any(_folded(marker) in haystack for marker in markers):
                return source_type
        if document.source_type and document.source_type != "unknown":
            return document.source_type
        return self.profile.default_source_type

    def date_context(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken] = ()
    ) -> DateContext | None:
        """Build the dating context, seeded with the screen's explicit dates.

        A list that mixes ``2026.08.14`` with a bare ``08.13`` states its own
        year. Feeding the explicit dates back in as surrounding rows dates the
        partial ones far more accurately than the upload date can, which
        matters most for screenshots uploaded long after the fact.
        """

        if document.uploaded_at is None:
            return None
        explicit = tuple(
            resolved.value
            for resolved in (resolve_explicit_date(token.text) for token in tokens)
            if resolved is not None and resolved.value is not None
        )
        return DateContext(
            uploaded_at=document.uploaded_at,
            time_zone=document.time_zone,
            statement_month=document.statement_month,
            surrounding_dates=explicit,
        )

    def is_screen_marker(self, text: str) -> bool:
        """Whether a token names the app or the screen rather than a transaction.

        Screen titles like ``이체완료`` contain direction words, so they must
        not be read as a row's own label — the source type they imply already
        carries that information.
        """

        folded = _folded(text)
        if not folded:
            return False
        return any(
            _folded(marker) in folded
            for marker in (
                *self.profile.institution_markers,
                *(
                    marker
                    for markers in self.profile.source_type_markers.values()
                    for marker in markers
                ),
            )
        )

    def merge_rows(
        self, rows: Sequence[Sequence[NormalizedToken]]
    ) -> list[tuple[NormalizedToken, ...]]:
        """Fold a key/value detail screen into one row, reading order preserved.

        A receipt spreads a single transaction over several lines, so the date,
        the amount, and the approval number each sit on their own row. Merging
        lets the row extractor see all of them at once.
        """

        merged = [token for row in rows for token in row]
        if not merged:
            return []
        merged.sort(key=lambda token: (token.bounding_box.top, token.bounding_box.left))
        return [tuple(merged)]

    def is_chrome_row(self, row: Sequence[NormalizedToken]) -> bool:
        """Whether a row is app chrome rather than a transaction."""

        text = " ".join(_folded(token.text) for token in row)
        if not text:
            return True
        return any(_folded(marker) in text for marker in self.profile.chrome_markers)

    # ------------------------------------------------------------------
    # Row extraction
    # ------------------------------------------------------------------

    def extract_row(
        self,
        row: Sequence[NormalizedToken],
        document: DocumentMetadata,
        context: DateContext | None,
    ) -> RowFields:
        resolved_date = self._resolve_row_date(row, context)
        amounts, balance, consumed = self._classify_amounts(row)
        amount_index, amount = amounts[0] if amounts else (None, None)
        suffix, suffix_index = self._instrument_suffix(row)
        approval_code, approval_indices = self._approval_code(row)
        installment_months, installment_indices = self._installment_months(row)
        structured = consumed | approval_indices | installment_indices
        if suffix_index is not None:
            structured.add(suffix_index)
        labels = tuple(
            _clean(token.text)
            for index, token in enumerate(row)
            if index not in consumed
            and not looks_like_date(token.text)
            and not self.is_screen_marker(token.text)
        )
        merchant = self.merchant_text(row, structured)
        return RowFields(
            row=tuple(row),
            resolved_date=resolved_date,
            amount=amount,
            balance=balance[1] if balance is not None else None,
            extra_amounts=tuple(candidate for _, candidate in amounts[1:]),
            labels=labels,
            merchant=merchant,
            instrument_suffix=suffix,
            approval_code=approval_code,
            installment_months=installment_months,
            amount_center=(
                _horizontal_center(row[amount_index]) if amount_index is not None else None
            ),
        )

    def _resolve_row_date(
        self, row: Sequence[NormalizedToken], context: DateContext | None
    ) -> ResolvedDate:
        for token in row:
            if not looks_like_date(token.text):
                continue
            if context is None:
                # Without an upload moment only explicit dates can be trusted.
                parsed = resolve_explicit_date(token.text)
                if parsed is not None and parsed.value is not None:
                    return parsed
                continue
            resolved = resolve_date(token.text, context)
            if resolved.value is not None:
                return resolved
        return ResolvedDate(None, None, 0.0, "no date token in row")

    def _classify_amounts(
        self, row: Sequence[NormalizedToken]
    ) -> tuple[
        tuple[tuple[int, MoneyCandidate], ...],
        tuple[int, MoneyCandidate] | None,
        set[int],
    ]:
        """Split a row's money tokens into the amount and the running balance."""

        consumed: set[int] = set()
        labelled_balance: tuple[int, MoneyCandidate] | None = None
        candidates: list[tuple[int, MoneyCandidate]] = []
        previous_was_balance_label = False
        for index, token in enumerate(row):
            text = _folded(token.text)
            # A partial date such as "08.14" is shaped like a decimal amount.
            # Dates win, so a row's date column is never read as money.
            if looks_like_date(token.text) or not looks_like_money(token.text):
                previous_was_balance_label = any(label in text for label in BALANCE_LABELS)
                continue
            candidate = parse_money(token.text, default_currency=self.profile.default_currency)
            if candidate is None:
                previous_was_balance_label = False
                continue
            consumed.add(index)
            is_balance = previous_was_balance_label or any(
                label in text for label in BALANCE_LABELS
            )
            previous_was_balance_label = False
            if is_balance and labelled_balance is None:
                labelled_balance = (index, candidate)
                continue
            candidates.append((index, candidate))

        if labelled_balance is not None:
            return tuple(candidates), labelled_balance, consumed
        if self.profile.balance_column and len(candidates) > 1:
            # The rightmost column of a bank list is the running balance.
            return tuple(candidates[:-1]), candidates[-1], consumed
        return tuple(candidates), None, consumed

    def _instrument_suffix(self, row: Sequence[NormalizedToken]) -> tuple[str | None, int | None]:
        """Read the visible trailing digits of a masked account or card number."""

        for index, token in enumerate(row):
            cleaned = _clean(token.text)
            if MASK_RUN_RE.search(cleaned) is None:
                continue
            groups = DIGIT_GROUP_RE.findall(cleaned)
            if groups:
                return groups[-1][-4:], index
        for index, token in enumerate(row):
            cleaned = _clean(token.text)
            folded = cleaned.casefold()
            if not any(marker in folded for marker in SUFFIX_CONTEXT):
                continue
            trailing = TRAILING_SUFFIX_RE.search(cleaned)
            if trailing is not None:
                return trailing.group(1), index
        return None, None

    def _approval_code(self, row: Sequence[NormalizedToken]) -> tuple[str | None, set[int]]:
        texts = [_clean(token.text) for token in row]
        for index, cleaned in enumerate(texts):
            folded = cleaned.casefold()
            if not any(label in folded for label in APPROVAL_LABELS):
                continue
            own = APPROVAL_DIGITS_RE.search(cleaned)
            if own is not None:
                return own.group(1), {index}
            if index + 1 < len(texts):
                following = APPROVAL_DIGITS_RE.fullmatch(texts[index + 1])
                if following is not None:
                    return following.group(1), {index, index + 1}
        return None, set()

    def _installment_months(self, row: Sequence[NormalizedToken]) -> tuple[int | None, set[int]]:
        """Read installment metadata, which cards print next to the amount."""

        indices = {
            index
            for index, token in enumerate(row)
            if any(
                label in _folded(token.text)
                for label in (*SINGLE_PAYMENT_LABELS, *INSTALLMENT_LABELS)
            )
        }
        if not indices:
            return None, set()
        joined = " ".join(_folded(token.text) for token in row)
        if any(label in joined for label in SINGLE_PAYMENT_LABELS):
            return 1, indices
        if not any(label in joined for label in INSTALLMENT_LABELS):
            return None, set()
        match = INSTALLMENT_RE.search(joined)
        if match is None:
            return None, indices
        months = int(match.group(1))
        return (months, indices) if months >= 1 else (None, indices)

    def merchant_text(self, row: Sequence[NormalizedToken], consumed: set[int]) -> str | None:
        """Join the tokens that name a counterparty, dropping every other kind.

        Structured values, direction labels, balance labels, and app chrome are
        all removed, so what remains is the merchant or counterparty as printed.
        """

        parts: list[str] = []
        for index, token in enumerate(row):
            if index in consumed or looks_like_date(token.text):
                continue
            cleaned = _clean(token.text)
            folded = cleaned.casefold()
            if not cleaned or cleaned.isdigit() or is_direction_label(cleaned):
                continue
            if folded in FIELD_LABELS or folded.replace(" ", "") in FIELD_LABELS:
                continue
            if any(label in folded for label in BALANCE_LABELS):
                continue
            if any(_folded(marker) in folded for marker in self.profile.institution_markers):
                continue
            if any(_folded(marker) in folded for marker in self.profile.layout_markers):
                continue
            if any(
                _folded(marker) in folded
                for markers in self.profile.source_type_markers.values()
                for marker in markers
            ):
                # Screen titles such as "이체완료" name the screen, not a payee.
                continue
            parts.append(cleaned)
        return " ".join(parts).strip() or None

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    def detect_direction_columns(
        self, rows: Sequence[Sequence[NormalizedToken]]
    ) -> DirectionColumns:
        """Locate the 출금 and 입금 column headers, if the layout has them.

        Only a row that carries both headers and no amount counts, so a
        transaction row labelled ``출금`` is never mistaken for a header.
        """

        for row in rows:
            debit_center: float | None = None
            credit_center: float | None = None
            carries_money = False
            for token in row:
                cleaned = _folded(token.text)
                if looks_like_money(token.text):
                    carries_money = True
                    break
                if cleaned in DEBIT_COLUMN_HEADERS and debit_center is None:
                    debit_center = _horizontal_center(token)
                elif cleaned in CREDIT_COLUMN_HEADERS and credit_center is None:
                    credit_center = _horizontal_center(token)
            if not carries_money and debit_center is not None and credit_center is not None:
                return DirectionColumns(debit_center, credit_center)
        return DirectionColumns()

    def resolve_row_direction(
        self,
        document: DocumentMetadata,
        fields: RowFields,
        columns: DirectionColumns | None = None,
        *,
        source_type: str | None = None,
    ) -> DirectionResolution:
        """Resolve one row's direction against the screenshot's source type.

        ``source_type`` is the type detected for the whole screenshot. Passing
        it matters: a statement's ``청구금액`` row carries no screen title of
        its own, so detecting the type from that row alone would lose the fact
        that the screen is a statement.
        """

        labels = list(fields.labels)
        if fields.amount is not None and fields.amount.source_label:
            labels.insert(0, fields.amount.source_label)
        if fields.installment_months is not None:
            labels.append("결제")
        resolution = resolve_direction(
            source_type=source_type
            or self.detect_source_type(
                document, " ".join(_folded(token.text) for token in fields.row)
            ),
            labels=labels,
            display_sign=fields.amount.source_sign if fields.amount is not None else "",
            instrument_type=document.instrument_type,
        )
        if not resolution.is_unknown or columns is None or fields.amount_center is None:
            return resolution
        by_column = columns.classify(fields.amount_center)
        if by_column is TransactionDirection.UNKNOWN:
            return resolution
        return replace(
            resolution,
            direction=by_column,
            confidence=COLUMN_DIRECTION_CONFIDENCE,
            reasons=(*resolution.reasons, f"amount sits in the {by_column.value} column"),
        )

    def validate_balances(
        self, fields: Sequence[RowFields], directions: Sequence[DirectionResolution]
    ) -> tuple[BalanceValidation, ...]:
        """Chain the visible balances, oldest row first.

        The chain uses the economic direction rather than the printed sign,
        because most Korean bank lists label rows ``출금``/``입금`` and print
        every amount unsigned.
        """

        paired = list(zip(fields, directions, strict=True))
        ordered = list(reversed(paired)) if self.profile.rows_newest_first else paired
        rows = [
            BalanceRow(
                signed_amount_minor=_signed_minor(item, direction),
                balance_after=item.balance.money if item.balance is not None else None,
            )
            for item, direction in ordered
        ]
        validations = validate_balance_chain(rows)
        return tuple(reversed(validations)) if self.profile.rows_newest_first else validations

    def build_observation(
        self,
        document: DocumentMetadata,
        fields: RowFields,
        direction: DirectionResolution,
        validation: BalanceValidation,
        header_suffix: str | None = None,
        *,
        source_type: str | None = None,
    ) -> ParsedObservation:
        amount = fields.amount
        money = amount.money if amount is not None else None
        ambiguous: set[str] = set()
        missing: set[str] = set()

        if amount is not None and amount.ambiguous:
            ambiguous.add("amount")
        if fields.extra_amounts:
            ambiguous.add("amount")
        if money is None and "amount" not in ambiguous:
            missing.add("amount")
        if fields.resolved_date.value is None:
            missing.add("date")
        if direction.is_unknown:
            missing.add("direction")
        if fields.merchant is None:
            missing.add("merchant")
        if validation.status is BalanceStatus.INVALID:
            ambiguous.add("balance_after")

        confidence_factors: dict[str, float | bool | str] = {
            "token_confidence": round(average_confidence(fields.row), 6),
            "row_token_count": len(fields.row),
            "date_confidence": round(fields.resolved_date.confidence, 6),
            "date_inference": (
                fields.resolved_date.inference.value
                if fields.resolved_date.inference is not None
                else "none"
            ),
            "amount_confidence": round(amount.confidence, 6) if amount is not None else 0.0,
            "direction_confidence": round(direction.confidence, 6),
            "balance_status": validation.status.value,
            "balance_confidence_delta": validation.confidence_delta,
            "parser_profile": self.profile.name,
        }
        if validation.difference_minor:
            confidence_factors["balance_difference_minor"] = validation.difference_minor

        observation = ParsedObservation(
            occurred_on=fields.resolved_date.value,
            amount=money.decimal_amount if money is not None else None,
            currency=str(money.resolved_currency) if money is not None else None,
            direction=direction.direction,
            merchant=fields.merchant,
            counterparty=(
                fields.merchant
                if source_type in {"bank_transfer_confirmation", "bank_transaction_detail"}
                else None
            ),
            instrument_suffix=fields.instrument_suffix or header_suffix,
            balance_after=_decimal(fields.balance),
            source_region=row_region(fields.row),
            confidence_factors=confidence_factors,
            missing_fields=frozenset(missing),
            ambiguous_fields=frozenset(ambiguous),
            display_sign=amount.source_sign if amount is not None else "",
            direction_label=direction.source_label,
            approval_code=fields.approval_code,
            installment_months=fields.installment_months,
            is_settlement=direction.is_settlement,
        )
        return replace(
            observation,
            confidence_factors={
                **observation.confidence_factors,
                # An inferred year is reviewable even when every field parsed.
                "requires_review": observation.blocks_automatic_confirmation
                or validation.status is BalanceStatus.INVALID
                or fields.resolved_date.requires_review,
            },
        )

    def unreadable_observation(
        self, row: Sequence[NormalizedToken], reason: str
    ) -> ParsedObservation:
        """Report a row the parser could not read, without losing the region."""

        return ParsedObservation(
            occurred_on=None,
            amount=None,
            currency=None,
            direction=TransactionDirection.UNKNOWN,
            merchant=None,
            source_region=row_region(row),
            confidence_factors={
                "parser_profile": self.profile.name,
                "parser_error": reason,
                "requires_review": True,
            },
            missing_fields=frozenset({"date", "amount", "direction", "merchant"}),
        )


def _horizontal_center(token: NormalizedToken) -> float:
    return (token.bounding_box.left + token.bounding_box.right) / 2


def _decimal(candidate: MoneyCandidate | None) -> Decimal | None:
    if candidate is None or candidate.money is None:
        return None
    return candidate.money.decimal_amount


def _signed_minor(fields: RowFields, direction: DirectionResolution) -> int | None:
    """The row amount signed by its economic direction, for balance chaining."""

    if fields.amount is None or fields.amount.money is None:
        return None
    magnitude = abs(fields.amount.money.amount_minor)
    if direction.direction is TransactionDirection.DEBIT:
        return -magnitude
    if direction.direction is TransactionDirection.CREDIT:
        return magnitude
    # An unknown direction cannot be chained; the printed sign is all there is.
    return fields.amount.signed_minor if fields.amount.source_sign else None


__all__ = [
    "InstitutionParser",
    "InstitutionProfile",
    "RowFields",
]
