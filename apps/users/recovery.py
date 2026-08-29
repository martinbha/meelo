from __future__ import annotations

import secrets

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from django_otp.plugins.otp_static.models import StaticDevice

from .models import RecoveryCode, User

RECOVERY_CODE_COUNT = 10


@transaction.atomic
def regenerate_recovery_codes(user: User) -> tuple[str, ...]:
    """Replace every prior code and return plaintext exactly once."""

    RecoveryCode.objects.filter(user=user).delete()
    StaticDevice.objects.filter(user=user).delete()
    raw_codes = (secrets.token_hex(8) for _ in range(RECOVERY_CODE_COUNT))
    codes = tuple(f"{code[:8]}-{code[8:]}" for code in raw_codes)
    RecoveryCode.objects.bulk_create(
        RecoveryCode(user=user, code_hash=make_password(code)) for code in codes
    )
    return codes


@transaction.atomic
def consume_recovery_code(user: User, candidate: str) -> bool:
    """Consume one matching unused code under a lock."""

    for code in RecoveryCode.objects.select_for_update().filter(user=user, used_at__isnull=True):
        if check_password(candidate, code.code_hash):
            code.used_at = timezone.now()
            code.save(update_fields=("used_at",))
            return True
    return False
