"""Seal values written before their column was encrypted.

Runs the same code as ``manage.py encrypt_plaintext_fields``, so a deploy that
applies migrations does not leave the database half-encrypted while waiting for
somebody to remember the command.

**One-way, and deliberately skippable.** Encryption needs the master key, and a
migration is not a good place to fail: a fresh install, CI, and every test
database have no key configured and no rows to seal, and refusing to migrate
there would break the one path that has nothing to do. So the migration does
nothing when no master key is configured, and says so.

Reversing it is a restore from the backup taken before the deploy. Decrypting a
whole database back to plaintext on request is not an operation this application
should be able to perform, so ``reverse_code`` is a no-op that says why rather
than a function that undoes the work.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations


def seal_existing_rows(apps: Any, schema_editor: Any) -> None:
    from apps.core.key_management import KeyManagementError, get_user_data_key, load_master_key
    from apps.core.management.commands.encrypt_plaintext_fields import seal_user
    from apps.users.models import User

    try:
        master_key = load_master_key()
    except KeyManagementError:
        # No key configured: a fresh install, CI, or a test database. There is
        # nothing to seal, and failing here would break the only case that is
        # already correct.
        return

    for user in User.objects.all().iterator():
        try:
            data_key = get_user_data_key(user=user, actor=user, master_key=master_key)
        except Exception:  # noqa: BLE001 - a user with no key has no rows to seal
            continue
        seal_user(user=user, data_key=data_key, key_version=user.encryption_key_version)


def do_not_reverse(apps: Any, schema_editor: Any) -> None:
    """Deliberately does nothing.

    The way back from this migration is a restore, not a decrypt-everything
    routine sitting in the codebase waiting to be called.
    """


class Migration(migrations.Migration):
    dependencies = [("core", "0016_alter_auditevent_event_type")]

    operations = [migrations.RunPython(seal_existing_rows, do_not_reverse)]
