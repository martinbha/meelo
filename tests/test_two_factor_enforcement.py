from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, local
from typing import Any
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection
from django.db.models import QuerySet
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import TOTP, totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core.models import AuditEvent
from apps.users.models import TwoFactorAttempt
from apps.users.recovery import regenerate_recovery_codes
from tests.factories import make_user

pytestmark = pytest.mark.django_db
PASSWORD = "two-factor-enforcement-password"
REMOTE_IP = "192.0.2.44"


def hashed_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()


@pytest.fixture
def owner() -> Any:
    return make_user(email="enforced@example.com", password=PASSWORD)


def test_enrolled_user_must_verify_before_authenticated_or_admin_pages(owner: Any) -> None:
    owner.is_staff = True
    owner.save(update_fields=("is_staff",))
    device = TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    client = Client()
    client.force_login(owner)

    for url in (reverse("transaction-list"), reverse("admin:index")):
        response = client.get(url)
        assert response.status_code == 302
        assert response["Location"] == reverse("two-factor-verify")

    code = str(totp(device.bin_key)).zfill(6)
    assert client.post(reverse("two-factor-verify"), {"token": code}).status_code == 302
    assert client.get(reverse("transaction-list")).status_code == 200
    assert client.get(reverse("admin:index")).status_code == 200


def test_failed_codes_are_audited_and_throttled(owner: Any) -> None:
    TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    client = Client(REMOTE_ADDR=REMOTE_IP)
    client.force_login(owner)

    for _ in range(5):
        assert (
            client.post(reverse("two-factor-verify"), {"token": "invalid-token"}).status_code == 400
        )
    assert client.post(reverse("two-factor-verify"), {"token": "invalid-token"}).status_code == 429
    assert AuditEvent.objects.filter(user=owner, event_type="two_factor_failure").count() == 5
    attempt = TwoFactorAttempt.objects.get(user=owner, ip_hash=hashed_ip(REMOTE_IP))
    assert attempt.failure_count == 5
    assert attempt.expires_at is not None and attempt.expires_at > timezone.now()
    assert TOTPDevice.objects.get(user=owner).throttling_failure_count > 0


def test_user_without_device_is_unaffected(owner: Any) -> None:
    client = Client()
    client.force_login(owner)

    assert client.get(reverse("transaction-list")).status_code == 200
    assert not TwoFactorAttempt.objects.filter(user=owner).exists()


def test_login_redirects_enrolled_user_to_verification_and_accepts_recovery_code(
    owner: Any,
) -> None:
    TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    recovery_code = regenerate_recovery_codes(owner)[0]
    client = Client()

    response = client.post(reverse("login"), {"username": owner.email, "password": PASSWORD})
    assert response["Location"] == reverse("two-factor-verify")

    response = client.post(reverse("two-factor-verify"), {"token": recovery_code})
    assert response.status_code == 302
    assert client.get(reverse("transaction-list")).status_code == 200


def test_expired_shared_attempt_budget_allows_a_fresh_valid_code(owner: Any) -> None:
    device = TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    client = Client(REMOTE_ADDR=REMOTE_IP)
    client.force_login(owner)
    TwoFactorAttempt.objects.create(
        user=owner,
        ip_hash=hashed_ip(REMOTE_IP),
        failure_count=5,
        expires_at=timezone.now() - timedelta(seconds=1),
    )

    code = str(totp(device.bin_key)).zfill(device.digits)
    response = client.post(reverse("two-factor-verify"), {"token": code})

    assert response.status_code == 302
    attempt = TwoFactorAttempt.objects.get(user=owner, ip_hash=hashed_ip(REMOTE_IP))
    assert attempt.failure_count == 0
    assert attempt.expires_at is not None and attempt.expires_at > timezone.now()


@pytest.mark.django_db(transaction=True)
def test_concurrent_clients_accept_a_totp_only_once(owner: Any) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL exercises concurrent device-row locks")

    fixed_time = 1_800_000_005
    counter = fixed_time // 30
    device = TOTPDevice.objects.create(
        user=owner,
        name="phone",
        confirmed=True,
        tolerance=0,
        last_t=counter - 1,
    )
    clock = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    clock.time = fixed_time
    token = str(clock.token()).zfill(device.digits)
    clients = [Client(REMOTE_ADDR=REMOTE_IP), Client(REMOTE_ADDR="198.51.100.19")]
    for client in clients:
        client.force_login(owner)
    barrier = Barrier(2)

    def submit(client: Client) -> int:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return client.post(reverse("two-factor-verify"), {"token": token}).status_code
        finally:
            close_old_connections()

    with (
        patch("django_otp.plugins.otp_totp.models.time.time", return_value=fixed_time),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        statuses = list(pool.map(submit, clients))

    assert sorted(statuses) == [302, 400]
    device.refresh_from_db()
    assert device.last_t == counter
    assert device.throttling_failure_count == 1
    attempts = list(TwoFactorAttempt.objects.filter(user=owner))
    assert len(attempts) == 2
    assert sorted(attempt.failure_count for attempt in attempts) == [0, 1]
    failures = AuditEvent.objects.filter(user=owner, event_type="two_factor_failure")
    assert failures.count() == 1
    assert failures.get().metadata == {"method": "otp"}


@pytest.mark.django_db(transaction=True)
def test_concurrent_invalid_codes_cannot_spend_the_last_attempt_twice(owner: Any) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL exercises shared attempt-row locks")

    TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    TwoFactorAttempt.objects.create(
        user=owner,
        ip_hash=hashed_ip(REMOTE_IP),
        failure_count=4,
        expires_at=timezone.now() + timedelta(minutes=1),
    )
    clients = [Client(REMOTE_ADDR=REMOTE_IP), Client(REMOTE_ADDR=REMOTE_IP)]
    for client in clients:
        client.force_login(owner)
    barrier = Barrier(2)

    def submit(client: Client) -> int:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return client.post(reverse("two-factor-verify"), {"token": "invalid-token"}).status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, clients))

    assert sorted(statuses) == [400, 429]
    attempt = TwoFactorAttempt.objects.get(user=owner, ip_hash=hashed_ip(REMOTE_IP))
    assert attempt.failure_count == 5
    assert AuditEvent.objects.filter(user=owner, event_type="two_factor_failure").count() == 1


@pytest.mark.django_db(transaction=True)
def test_expiring_counter_cannot_be_deleted_between_lookup_and_lock(
    owner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL exercises the counter-expiry locking race")

    TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    real_now = timezone.now
    first_time = real_now() + timedelta(seconds=30)
    second_time = first_time + timedelta(seconds=2)
    TwoFactorAttempt.objects.create(
        user=owner,
        ip_hash=hashed_ip(REMOTE_IP),
        failure_count=4,
        expires_at=first_time + timedelta(seconds=1),
    )

    first_client = Client(REMOTE_ADDR=REMOTE_IP)
    second_client = Client(REMOTE_ADDR=REMOTE_IP)
    first_client.force_login(owner)
    second_client.force_login(owner)

    request_state = local()
    first_selected = Event()
    second_started = Event()
    release_first = Event()
    original_get_or_create = QuerySet.get_or_create

    def controlled_now() -> Any:
        return getattr(request_state, "now", None) or real_now()

    def delay_first_lookup(queryset: Any, *args: Any, **kwargs: Any) -> Any:
        if queryset.model is TwoFactorAttempt and getattr(request_state, "first", False):
            result = original_get_or_create(queryset, *args, **kwargs)
            first_selected.set()
            if not release_first.wait(timeout=10):
                raise TimeoutError("First OTP request did not resume.")
            return result
        if queryset.model is TwoFactorAttempt and getattr(request_state, "second", False):
            second_started.set()
        return original_get_or_create(queryset, *args, **kwargs)

    monkeypatch.setattr(timezone, "now", controlled_now)
    monkeypatch.setattr(QuerySet, "get_or_create", delay_first_lookup)

    def submit(client: Client, request_time: Any, *, first: bool) -> int:
        close_old_connections()
        request_state.now = request_time
        request_state.first = first
        request_state.second = not first
        try:
            return client.post(reverse("two-factor-verify"), {"token": "invalid-token"}).status_code
        finally:
            request_state.now = None
            request_state.first = False
            request_state.second = False
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(submit, first_client, first_time, first=True)
        assert first_selected.wait(timeout=5)
        second_result = pool.submit(submit, second_client, second_time, first=False)
        try:
            assert second_started.wait(timeout=5)
        finally:
            release_first.set()
        statuses = [first_result.result(timeout=10), second_result.result(timeout=10)]

    assert sorted(statuses) == [400, 429]
    attempt = TwoFactorAttempt.objects.get(user=owner, ip_hash=hashed_ip(REMOTE_IP))
    assert attempt.failure_count == 5
    assert AuditEvent.objects.filter(user=owner, event_type="two_factor_failure").count() == 1


@pytest.mark.django_db(transaction=True)
def test_attempt_budget_is_shared_between_worker_processes(owner: Any) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL exercises counters shared across worker processes")

    TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    child = textwrap.dedent(
        """
        import os
        import sys

        os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.ci"
        os.environ["POSTGRES_TEST_DB"] = sys.argv[1]
        import django
        django.setup()

        from django.contrib.auth import get_user_model
        from django.test import Client
        from django.urls import reverse

        user = get_user_model().objects.get(pk=int(sys.argv[2]))
        client = Client(REMOTE_ADDR=sys.argv[4])
        client.force_login(user)
        statuses = [
            client.post(reverse("two-factor-verify"), {"token": "invalid-token"}).status_code
            for _ in range(int(sys.argv[3]))
        ]
        print(",".join(str(status) for status in statuses))
        """
    )
    environment = os.environ.copy()
    environment["DJANGO_SETTINGS_MODULE"] = "config.settings.ci"
    test_database = str(connection.settings_dict["NAME"])

    def worker_attempts(count: int) -> list[int]:
        result = subprocess.run(
            [sys.executable, "-c", child, test_database, str(owner.pk), str(count), REMOTE_IP],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return [int(status) for status in result.stdout.strip().split(",")]

    assert worker_attempts(3) == [400, 400, 400]
    assert worker_attempts(2) == [400, 400]
    assert worker_attempts(1) == [429]

    attempt = TwoFactorAttempt.objects.get(user=owner, ip_hash=hashed_ip(REMOTE_IP))
    assert attempt.failure_count == 5
    assert AuditEvent.objects.filter(user=owner, event_type="two_factor_failure").count() == 5
