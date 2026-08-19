"""Rotating the blind-index key, and rebuilding every token under the new one.

The search key is separate from the encryption key (#161), which is what makes
this an operation of its own rather than a side effect of something else. It is
also what makes it safe: nothing here decrypts anything, and nothing here
touches a value. Only the tokens beside them move.

The window is the whole problem. Between a new key becoming active and the last
token being rebuilt, a table holds tokens under two keys, and a lookup that
knows about only one of them finds half the rows and reports that as an answer.
Nobody files a bug against a search that quietly returned less than it should.

So three things hold together:

**Every token says which key built it.** The prefix is the search key version,
readable without holding either key. That is what lets a reindex select the
stale ones and a query recognise both.

**Lookups search both versions during the window.** :func:`index_candidates`
builds one token per live key version and the caller matches any of them.
Retiring the old key is what ends that, and it happens only once nothing is
indexed under it.

**The rebuild is resumable and reports progress.** Same checkpointing as the
data-key rotation, under its own ``key_kind`` so the two cannot be confused for
one another.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_transaction
from django.db.models import Q

from apps.core.blind_index import index_version
from apps.core.key_management import (
    active_search_key_version,
    get_user_data_key,
    get_user_search_keys,
    load_master_key,
    provision_next_search_key,
)
from apps.core.management.commands.backfill_blind_indexes import INDEXED_MODELS, IndexedModel
from apps.core.rotation import _record_progress, resume_point
from apps.users.models import User, UserSearchKey

SEARCH_KIND = "search"


def index_candidates(
    build: Any, value: str, *, user_id: Any, keys: dict[int, bytes]
) -> tuple[str, ...]:
    """One token per live search key version, for a lookup to match any of.

    During a rotation this returns two. Outside one it returns a single token
    and the query is exactly what it always was, so the transition costs nothing
    once it is over.
    """

    tokens = []
    for version in sorted(keys, reverse=True):
        token = build(value, user_id=user_id, key=keys[version])
        if token:
            tokens.append(token)
    return tuple(dict.fromkeys(tokens))


def any_version(column: str, tokens: Sequence[str]) -> Q:
    """A filter matching a column against every live version of one value."""

    query = Q(pk__in=[])
    for token in tokens:
        query |= Q(**{column: token})
    return query


@dataclass(slots=True)
class ReindexReport:
    rows_examined: int = 0
    tokens_rebuilt: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.failures


def reindex_user(
    *,
    user: User,
    data_key: bytes,
    search_key: bytes,
    key_version: int,
    models: Sequence[IndexedModel] = INDEXED_MODELS,
    batch_size: int = 500,
    dry_run: bool = False,
) -> ReindexReport:
    """Rebuild every token that is not already under ``key_version``."""

    report = ReindexReport()
    for spec in models:
        model = spec.model()
        queryset = model.objects.filter(**{spec.owner_filter: user.pk})
        if not dry_run:
            cursor = resume_point(
                user=user, new_version=key_version, label=spec.label, kind=SEARCH_KIND
            )
            if cursor:
                queryset = queryset.filter(pk__gt=cursor)
        last_pk: Any = None
        while True:
            page = queryset.order_by("pk")
            if last_pk is not None:
                page = page.filter(pk__gt=last_pk)
            batch = list(page[:batch_size])
            if not batch:
                break
            with db_transaction.atomic():
                for record in batch:
                    report.rows_examined += 1
                    updates: dict[str, str] = {}
                    for column in spec.columns:
                        stored = getattr(record, column.index_column) or ""
                        if stored and index_version(stored) == key_version:
                            continue
                        try:
                            plaintext = record.read_field(column.source_column, key=data_key)
                        except Exception as error:  # noqa: BLE001 - reported, not raised
                            report.failures.append(
                                f"{spec.label}:{record.pk}:{column.source_column}: {error}"
                            )
                            continue
                        rebuilt = column.indexer(plaintext, user_id=record.user_id, key=search_key)
                        if rebuilt != stored:
                            updates[column.index_column] = rebuilt
                    if updates and not dry_run:
                        model.objects.filter(pk=record.pk).update(**updates)
                    report.tokens_rebuilt += len(updates)
                if not dry_run:
                    _record_progress(
                        user=user,
                        new_version=key_version,
                        label=spec.label,
                        last_record_id=str(batch[-1].pk),
                        rows=len(batch),
                        complete=False,
                        kind=SEARCH_KIND,
                    )
            last_pk = batch[-1].pk
        if not dry_run:
            _record_progress(
                user=user,
                new_version=key_version,
                label=spec.label,
                last_record_id="",
                rows=0,
                complete=True,
                kind=SEARCH_KIND,
            )
    return report


def stale_token_count(*, user: User, key_version: int) -> int:
    """How many tokens are still under a key other than the current one."""

    remaining = 0
    for spec in INDEXED_MODELS:
        model = spec.model()
        for record in model.objects.filter(**{spec.owner_filter: user.pk}).iterator():
            for column in spec.columns:
                stored = getattr(record, column.index_column) or ""
                if stored and index_version(stored) != key_version:
                    remaining += 1
    return remaining


class Command(BaseCommand):
    help = "Rotate the blind-index search key and rebuild every token under it."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--email", help="Rotate one user only.")
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be rebuilt, without a new key or any writes.",
        )
        parser.add_argument(
            "--retire",
            action="store_true",
            help="Remove superseded search keys, but only once no token uses them.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        master_key = load_master_key()
        batch_size = max(int(options["batch_size"]), 1)
        users = User.objects.all().order_by("email")
        if options["email"]:
            users = users.filter(email=str(options["email"]).strip().lower())
            if not users.exists():
                raise CommandError(f"No user exists for {options['email']}.")

        for user in users:
            self._rotate(
                user,
                master_key=master_key,
                batch_size=batch_size,
                dry_run=bool(options["dry_run"]),
                retire=bool(options["retire"]),
            )

    def _rotate(
        self, user: User, *, master_key: bytes, batch_size: int, dry_run: bool, retire: bool
    ) -> None:
        current = active_search_key_version(user=user)
        if not current:
            self.stdout.write(f"{user.email}: no search key, nothing to rotate.")
            return

        data_key = get_user_data_key(user=user, actor=user, master_key=master_key)
        if dry_run:
            keys = get_user_search_keys(user=user, master_key=master_key)
            report = reindex_user(
                user=user,
                data_key=data_key,
                search_key=keys[current],
                key_version=current,
                batch_size=batch_size,
                dry_run=True,
            )
            self.stdout.write(
                f"{user.email}: {report.rows_examined} row(s) would be examined; "
                f"{report.tokens_rebuilt} token(s) are not at version {current}. "
                "Nothing was written."
            )
            return

        if retire:
            self._retire(user, key_version=current)
            return

        # The new key goes active first. Tokens written from this moment are
        # already correct, and the rebuild only has to catch up with history.
        record = provision_next_search_key(user=user, actor=user, master_key=master_key)
        keys = get_user_search_keys(user=user, master_key=master_key)
        report = reindex_user(
            user=user,
            data_key=data_key,
            search_key=keys[record.version],
            key_version=record.version,
            batch_size=batch_size,
        )
        self.stdout.write(
            f"{user.email}: v{current} -> v{record.version}, rebuilt "
            f"{report.tokens_rebuilt} token(s) across {report.rows_examined} value(s)."
        )
        for failure in report.failures:
            self.stderr.write(f"  failed {failure}")
        remaining = stale_token_count(user=user, key_version=record.version)
        if remaining:
            self.stderr.write(
                f"{user.email}: {remaining} token(s) are still on an older key. "
                "Run again to finish before retiring anything."
            )
            return
        self.stdout.write(
            f"{user.email}: every token is at version {record.version}. "
            "Re-run with --retire to remove the superseded key."
        )

    def _retire(self, user: User, *, key_version: int) -> None:
        remaining = stale_token_count(user=user, key_version=key_version)
        if remaining:
            raise CommandError(
                f"{user.email}: {remaining} token(s) are still indexed under an older "
                "search key. Retiring it now would make them unsearchable forever."
            )
        removed = UserSearchKey.objects.filter(user=user, version__lt=key_version).delete()
        self.stdout.write(
            f"{user.email}: no token remains on an older key; "
            f"retired {removed[0]} superseded search key(s)."
        )
