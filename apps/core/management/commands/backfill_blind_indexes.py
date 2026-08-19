"""Rebuild the blind indexes rows are missing, in batches, safely repeatable.

Rows written before a column was indexed — or before the search key was
separated from the data key (#161) — carry an empty or stale token. An empty
token is not a small problem: the lookup that should find the row returns
nothing, and "nothing" is indistinguishable from "no such merchant". Nobody
reports a bug against a search that quietly found less than it should.

Two properties this command has to have, and both come from how it selects work
rather than from bookkeeping it keeps:

**Idempotent.** It computes what the token *should* be and writes only when the
stored value differs. Running it twice writes nothing the second time, so a
cron entry that overlaps itself is not a problem.

**Resumable.** It selects the rows that still need work, in primary-key order,
in bounded batches. An interruption leaves the finished rows finished, and the
next run picks up the remainder — there is no cursor to lose, because the
remaining work is a query rather than a position.

Progress is reported per model, so an operator watching a long run can see it
moving rather than guessing.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_transaction

from apps.core.key_management import get_user_search_key, load_master_key
from apps.core.searchable import approval_code_index, counterparty_index, identifier_index
from apps.users.models import User


class Indexer(Protocol):
    """How a stored value becomes a token, given its owner and the search key."""

    def __call__(self, value: str, *, user_id: Any, key: bytes) -> str: ...


def _merchant(value: str, *, user_id: Any, key: bytes) -> str:
    from apps.categorization.normalization import merchant_blind_index

    return merchant_blind_index(value, user_id=user_id, key=key) if value else ""


@dataclass(frozen=True, slots=True)
class IndexedColumn:
    """One blind-index column and the encrypted column it is built from."""

    index_column: str
    source_column: str
    indexer: Indexer


@dataclass(frozen=True, slots=True)
class IndexedModel:
    label: str
    columns: tuple[IndexedColumn, ...]
    owner_filter: str = "user_id"

    def model(self) -> Any:
        from django.apps import apps

        app_label, model_name = self.label.split(".")
        return apps.get_model(app_label, model_name)


#: Every column the backfill maintains. Kept beside the rotation list rather
#: than merged into it: rotation re-encrypts under a new *data* key, this
#: rebuilds tokens under the *search* key, and the two move independently
#: (specification 22.4, 22.6).
INDEXED_MODELS: tuple[IndexedModel, ...] = (
    IndexedModel(
        "transactions.CanonicalTransaction",
        (
            IndexedColumn("merchant_blind_index", "merchant_encrypted", _merchant),
            IndexedColumn("counterparty_blind_index", "counterparty_encrypted", counterparty_index),
        ),
    ),
    IndexedModel(
        "observations.ImportedObservation",
        (
            IndexedColumn("merchant_blind_index", "merchant_raw_encrypted", _merchant),
            IndexedColumn(
                "approval_code_blind_index", "approval_code_encrypted", approval_code_index
            ),
        ),
    ),
    IndexedModel(
        "financial_accounts.FinancialAccount",
        (IndexedColumn("identifier_blind_index", "masked_identifier_encrypted", identifier_index),),
    ),
)


@dataclass(slots=True)
class BackfillReport:
    rows_examined: int = 0
    tokens_written: int = 0
    rows_unreadable: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.rows_unreadable


def _batches(queryset: Any, size: int) -> Iterator[Sequence[Any]]:
    """Primary-key ordered pages, walked with a keyset cursor.

    A cursor on the primary key rather than ``OFFSET``: the rows are being
    written as they are read, and an offset into a changing result set skips
    rows. It also means each page is a bounded query rather than one that gets
    slower the further in it gets.
    """

    last_pk: Any = None
    while True:
        page = queryset.order_by("pk")
        if last_pk is not None:
            page = page.filter(pk__gt=last_pk)
        batch = list(page[:size])
        if not batch:
            return
        yield batch
        last_pk = batch[-1].pk


def backfill_user(
    *,
    user: User,
    data_key: bytes,
    search_key: bytes,
    models: Sequence[IndexedModel] = INDEXED_MODELS,
    batch_size: int = 500,
    dry_run: bool = False,
) -> BackfillReport:
    """Rebuild every stale or missing token for one user."""

    report = BackfillReport()
    for spec in models:
        model = spec.model()
        for column in spec.columns:
            queryset = model.objects.filter(**{spec.owner_filter: user.pk})
            for batch in _batches(queryset, batch_size):
                pending: list[Any] = []
                with db_transaction.atomic():
                    for record in batch:
                        report.rows_examined += 1
                        try:
                            plaintext = record.read_field(column.source_column, key=data_key)
                        except Exception as error:  # noqa: BLE001 - reported, not raised
                            report.rows_unreadable.append(
                                f"{spec.label}:{record.pk}:{column.source_column}: {error}"
                            )
                            continue
                        expected = column.indexer(plaintext, user_id=record.user_id, key=search_key)
                        if getattr(record, column.index_column) == expected:
                            continue
                        setattr(record, column.index_column, expected)
                        pending.append(record)
                    if pending and not dry_run:
                        model.objects.bulk_update(pending, [column.index_column])
                    report.tokens_written += len(pending)
    return report


class Command(BaseCommand):
    help = "Rebuild missing or stale blind indexes for one user or for everyone."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--email", help="Limit the backfill to one user.")
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        batch_size = max(int(options["batch_size"]), 1)
        master_key = load_master_key()
        users = User.objects.all().order_by("email")
        if options["email"]:
            users = users.filter(email=str(options["email"]).strip().lower())
            if not users.exists():
                raise CommandError(f"No user exists for {options['email']}.")

        from apps.core.key_management import get_user_data_key

        for user in users:
            data_key = get_user_data_key(user=user, actor=user, master_key=master_key)
            search_key = get_user_search_key(user=user, actor=user, master_key=master_key)
            report = backfill_user(
                user=user,
                data_key=data_key,
                search_key=search_key,
                batch_size=batch_size,
                dry_run=bool(options["dry_run"]),
            )
            verb = "would write" if options["dry_run"] else "wrote"
            self.stdout.write(
                f"{user.email}: examined {report.rows_examined} value(s), "
                f"{verb} {report.tokens_written} token(s)."
            )
            for problem in report.rows_unreadable:
                self.stderr.write(f"  unreadable {problem}")
