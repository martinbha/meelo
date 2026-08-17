"""Measuring what a report costs, so the decision to cache is evidence-based.

Every amount in this system is encrypted per user, which means the database
cannot add anything up and the application has to decrypt row by row. That is a
real cost, and the tempting fix — caching the totals — is the one thing this
design cannot afford: a cached total is a plaintext copy of somebody's finances
sitting outside the encrypted store (specification 22.5, 25.4).

So instead of guessing, this module measures. It splits a report into the three
things that take time and reports them separately, because the answer to "is this
too slow" is different depending on which one dominates:

- **query** — fetching rows. Fixed by indexes and narrower predicates.
- **decrypt** — one AES-GCM open per row. Fixed only by decrypting fewer rows.
- **aggregate** — the arithmetic. Never the problem, and worth measuring so that
  is visible rather than assumed.

A snapshot table would only be justified once *decrypt* dominates at a volume a
person actually has, and even then it would have to be encrypted itself. The
thresholds in :data:`BUDGET` are what "too slow" means until somebody measures
otherwise.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from apps.transactions.models import CanonicalTransaction

from .amounts import transaction_amount
from .grouping import amount_in
from .spending import SpendingTotals, accumulate_amounts, reportable_transactions

#: What a month's report may cost before it needs attention, in milliseconds per
#: 1,000 transactions. Generous on purpose: these are the numbers that decide
#: whether to add a cache, and a tight budget would argue for one prematurely.
BUDGET: dict[str, float] = {
    "query_ms": 250.0,
    "decrypt_ms": 750.0,
    "aggregate_ms": 100.0,
    "total_ms": 1000.0,
}

#: Volume at which the decrypt cost would justify revisiting the design. Below
#: it, decrypting per request is cheaper than the risk a cache introduces.
SNAPSHOT_REVIEW_THRESHOLD = 50_000


@dataclass(frozen=True, slots=True)
class ReportTimings:
    """Where a report's time went, and how much work it did."""

    row_count: int
    query_ms: float
    decrypt_ms: float
    aggregate_ms: float

    @property
    def total_ms(self) -> float:
        return self.query_ms + self.decrypt_ms + self.aggregate_ms

    def per_thousand(self, name: str) -> float:
        """One measurement scaled to 1,000 rows, for comparison with the budget."""

        measured = float(getattr(self, name))
        if self.row_count == 0:
            return 0.0
        return measured * 1000.0 / self.row_count

    @property
    def dominant_cost(self) -> str:
        """Which of the three costs most. What to fix, if anything needs fixing."""

        costs = {
            "query": self.query_ms,
            "decrypt": self.decrypt_ms,
            "aggregate": self.aggregate_ms,
        }
        return max(costs, key=lambda name: costs[name])

    def within_budget(self, budget: dict[str, float] | None = None) -> bool:
        limits = budget or BUDGET
        return not self.exceeded(limits)

    def exceeded(self, budget: dict[str, float] | None = None) -> tuple[str, ...]:
        """Which budgets were exceeded, for a failure message worth reading."""

        limits = budget or BUDGET
        return tuple(
            f"{name}: {self.per_thousand(name):.1f}ms per 1000 rows exceeds {limit}ms"
            for name, limit in limits.items()
            if self.per_thousand(name) > limit
        )

    @property
    def snapshots_would_help(self) -> bool:
        """Whether a stored snapshot is worth its risk at this volume.

        False below :data:`SNAPSHOT_REVIEW_THRESHOLD` regardless of timing: a
        snapshot is a plaintext total outside the encrypted store, and paying
        that price to save a few hundred milliseconds is the wrong trade.
        """

        return self.row_count >= SNAPSHOT_REVIEW_THRESHOLD and self.dominant_cost == "decrypt"


def _elapsed_ms(work: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = work()
    return result, (time.perf_counter() - started) * 1000.0


def measure_report(
    user: Any,
    *,
    start: date | None = None,
    end: date | None = None,
    currency: str = "KRW",
    data_key: bytes | None = None,
) -> tuple[SpendingTotals, ReportTimings]:
    """Build one period's totals and report what each stage cost.

    The stages are timed separately rather than end to end, so a slow report
    names its own cause instead of inviting a guess.
    """

    resolved = currency.upper()
    rows, query_ms = _elapsed_ms(
        lambda: list(reportable_transactions(user, start=start, end=end).filter(currency=resolved))
    )
    # Decrypted exactly once. Timing the aggregation over rows it had to decrypt
    # again would charge the arithmetic for the cryptography and hide the real
    # cost — which is the one thing this module must not do.
    pairs, decrypt_ms = _elapsed_ms(
        lambda: [(row, amount_in(row, data_key=data_key, currency=resolved)) for row in rows]
    )
    totals, aggregate_ms = _elapsed_ms(lambda: accumulate_amounts(pairs))
    return (
        totals.get(resolved, SpendingTotals(resolved)),
        ReportTimings(
            row_count=len(pairs),
            query_ms=query_ms,
            decrypt_ms=decrypt_ms,
            aggregate_ms=aggregate_ms,
        ),
    )


def decrypted_total(
    transactions: Sequence[CanonicalTransaction], *, data_key: bytes | None = None
) -> int:
    """Sum every amount, whatever its type. A control for correctness tests.

    Deliberately ignores the reporting buckets: this is the number a person with
    a calculator would reach, which is what a totals bug has to be checked
    against.
    """

    return sum(
        transaction_amount(transaction, data_key=data_key).amount_minor
        for transaction in transactions
    )
