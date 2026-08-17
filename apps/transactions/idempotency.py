"""Making the creation of a confirmed transaction safe to repeat.

Every path that turns reviewed evidence into financial history can be attempted
twice: a worker retried after a timeout, a reviewer double-clicking, two browser
tabs open on the same queue. Each of those must end with *one* transaction, and
row locks alone do not guarantee it — a lock only helps while both attempts are
inside the same database, holding the same lock, and neither has crashed.

So each origin carries a deterministic key, and the database refuses a second
row for the same one. The lock makes the common case cheap; the constraint makes
the uncommon case correct.

Keys name their origin and its identifier and nothing else. A key is stored in
clear, so it must never carry an amount, a merchant, or anything else a database
reader should not learn.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError
from django.db import transaction as db_transaction

from .models import CanonicalTransaction

#: An observation a reviewer accepted on its own.
OBSERVATION_SOURCE = "observation"
#: Both sides of one internal transfer, keyed by the match that joined them.
TRANSFER_SOURCE = "transfer"
#: A refund confirmed against the purchase it reverses.
REFUND_SOURCE = "refund"


def source_key(source: str, identifier: Any) -> str:
    """Build the key for one origin, e.g. ``observation:3f2a…``."""

    if not source:
        raise ValueError("An idempotency key needs a source.")
    return f"{source}:{identifier}"


def save_once(canonical: CanonicalTransaction) -> tuple[CanonicalTransaction, bool]:
    """Save a transaction, or return the one an earlier attempt already made.

    Returns the transaction and whether this call created it. A row without a
    key — manual entry, which has no natural origin to key on — is saved
    directly, because two manual entries of the same amount on the same day are
    a legitimate thing for a person to do.
    """

    if not canonical.source_idempotency_key:
        canonical.save()
        return canonical, True

    try:
        # A savepoint, so losing the race does not poison the surrounding
        # transaction the caller is still using.
        with db_transaction.atomic():
            canonical.save()
    except IntegrityError:
        existing = CanonicalTransaction.objects.filter(
            user_id=canonical.user_id,
            source_idempotency_key=canonical.source_idempotency_key,
        ).first()
        if existing is None:
            raise
        return existing, False
    return canonical, True
