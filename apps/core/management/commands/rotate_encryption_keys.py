"""Rotate one or every user's data key, then verify before retiring the old one.

The order is the whole design:

1. **Provision the new key and make it active.** Everything written from that
   moment is already correct, so rotation only has to catch up with history
   rather than chase writes that are still arriving under the old key.
2. **Move the values, in bounded batches.** Each envelope carries its key
   version, so an interrupted run resumes by simply running again — rows already
   moved are skipped.
3. **Verify.** Every value is read back under the new key.
4. **Retire the old key only if step 3 was clean.** An old key deleted while one
   row still needs it is not a degraded system; it is a row nobody can ever read.

Retiring is opt-in (``--retire``) and refuses on any failure, because the safe
state after a partial rotation is "both keys exist" and the dangerous one is
"the only key that could read this row is gone".

**Run this with the web and worker processes stopped.** Between steps 1 and 2 the
active key cannot read rows that have not moved yet, so a request arriving mid-
rotation would fail. The alternative ordering — move first, activate after —
would leave writes during the rotation sealed under the key being retired, which
is worse: a correctness problem instead of a few minutes of downtime.
"""

from __future__ import annotations

import os
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_transaction
from django.utils import timezone

from apps.core.audit import record_audit_event
from apps.core.key_management import (
    KEY_SIZE,
    get_user_data_key,
    get_user_search_key,
    load_master_key,
    wrap_data_key,
)
from apps.core.rotation import (
    DEFAULT_BATCH_SIZE,
    rotate_user,
    verify_user,
)
from apps.users.models import User, UserDataKey


class Command(BaseCommand):
    help = "Rotate encryption keys, re-encrypt stored values, and verify the result."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--email", help="Rotate one user only.")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help="Rows re-encrypted per transaction.",
        )
        parser.add_argument(
            "--verify-only",
            action="store_true",
            help="Check that every value opens under the active key, changing nothing.",
        )
        parser.add_argument(
            "--retire",
            action="store_true",
            help="Delete superseded key versions, but only after a clean verification.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        master_key = load_master_key()
        users = User.objects.all()
        if options["email"]:
            users = users.filter(email=options["email"])
            if not users.exists():
                raise CommandError(f"No user with email {options['email']!r}.")

        for user in users.order_by("pk"):
            if options["verify_only"]:
                self._verify(user, master_key=master_key, batch_size=options["batch_size"])
                continue
            self._rotate(
                user,
                master_key=master_key,
                batch_size=options["batch_size"],
                retire=options["retire"],
            )

    def _active(self, user: Any) -> UserDataKey | None:
        return UserDataKey.objects.filter(user=user, is_active=True).first()

    def _verify(self, user: Any, *, master_key: bytes, batch_size: int) -> None:
        active = self._active(user)
        if active is None:
            self.stdout.write(f"{user.email}: no active key, nothing to verify.")
            return
        key = get_user_data_key(user=user, actor=user, master_key=master_key)
        report = verify_user(
            user=user, key=key, expected_version=active.version, batch_size=batch_size
        )
        self.stdout.write(
            f"{user.email}: checked {report.values_checked} value(s) "
            f"across {report.rows_checked} row(s) at version {active.version}."
        )
        for problem in report.unreadable:
            self.stderr.write(f"  unreadable {problem}")
        for problem in report.stale_versions:
            self.stderr.write(f"  not yet rotated {problem}")

    def _rotate(self, user: Any, *, master_key: bytes, batch_size: int, retire: bool) -> None:
        current = self._active(user)
        if current is None:
            self.stdout.write(f"{user.email}: no active key, skipping.")
            return

        old_key = get_user_data_key(user=user, actor=user, master_key=master_key)
        new_version = current.version + 1
        new_key = os.urandom(KEY_SIZE)

        # The new key goes active before a single value moves. Writes arriving
        # during the rotation then land under the key rotation is moving toward,
        # rather than under the one it is moving away from.
        with db_transaction.atomic():
            UserDataKey.objects.create(
                user=user,
                version=new_version,
                wrapped_key=wrap_data_key(
                    new_key, master_key=master_key, user_id=user.pk, version=new_version
                ),
                is_active=False,
            )
            UserDataKey.objects.filter(pk=current.pk).update(
                is_active=False, retired_at=timezone.now()
            )
            UserDataKey.objects.filter(user=user, version=new_version).update(is_active=True)
            record_audit_event(
                user=user,
                event_type="encryption_key_rotated",
                metadata={"from_version": current.version, "to_version": new_version},
            )

        report = rotate_user(
            user=user,
            old_key=old_key,
            new_key=new_key,
            new_version=new_version,
            search_key=get_user_search_key(user=user, actor=user, master_key=master_key),
            batch_size=batch_size,
        )
        self.stdout.write(
            f"{user.email}: v{current.version} -> v{new_version}, "
            f"{report.fields_rewritten} value(s) across {report.rows_rewritten} row(s), "
            f"{report.indexes_rebuilt} index(es) rebuilt."
        )
        for failure in report.failures:
            self.stderr.write(f"  failed {failure}")

        verification = verify_user(
            user=user, key=new_key, expected_version=new_version, batch_size=batch_size
        )
        if not verification.is_clean:
            for problem in [*verification.unreadable, *verification.stale_versions]:
                self.stderr.write(f"  unverified {problem}")
            self.stderr.write(
                f"{user.email}: verification failed; the old key is kept so nothing "
                f"becomes unreadable. Run again to finish the rotation."
            )
            return

        if retire:
            # Only now. Everything has been read back under the new key, so no
            # row still depends on the one being removed.
            removed = UserDataKey.objects.filter(user=user, version__lt=new_version).delete()
            self.stdout.write(f"{user.email}: retired {removed[0]} superseded key(s).")
        else:
            self.stdout.write(
                f"{user.email}: verified. Re-run with --retire to remove version {current.version}."
            )
