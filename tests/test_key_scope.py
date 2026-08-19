"""One unwrap per request, and nothing left behind (#159, specification 21.4, 22.2).

Three claims, each checked against behaviour rather than against the resolver's
own bookkeeping:

- a page that decrypts forty fields unwraps once and audits once,
- the key is gone once the request or the job ends, whatever happened in between,
- and a scope open for one person never answers for another.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.core import key_management
from apps.core.key_management import provision_user_data_key
from apps.core.key_scope import (
    KeyScopeError,
    clear_scope,
    current_scope,
    data_key_scope,
    request_data_key,
    resolve_data_key,
)
from apps.core.models import AuditEvent
from tests.factories import make_account, make_transaction, make_user

pytestmark = pytest.mark.django_db

PASSWORD = "scope-password"


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="scope-owner@example.com", password=PASSWORD)
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture(autouse=True)
def _no_leftover_scope() -> Any:
    """Every test starts and ends with nothing held."""

    clear_scope()
    yield
    clear_scope()


def unwrap_count(monkeypatch: Any) -> list[int]:
    """Count actual unwraps, not calls to the resolver."""

    calls = [0]
    original = key_management.unwrap_data_key

    def counting(*args: Any, **kwargs: Any) -> bytes:
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(key_management, "unwrap_data_key", counting)
    return calls


# ----------------------------------------------------------------------
# Once per scope
# ----------------------------------------------------------------------


def test_a_scope_unwraps_once_however_many_times_it_is_asked(
    owner: Any, master_key: bytes, monkeypatch: Any
) -> None:
    calls = unwrap_count(monkeypatch)

    with data_key_scope(user=owner, actor=owner, master_key=master_key) as scope:
        keys = [resolve_data_key(user=owner, actor=owner) for _ in range(40)]

    assert calls[0] == 1
    assert all(key == scope.data_key for key in keys)


def test_a_scope_audits_once_rather_than_once_per_field(owner: Any, master_key: bytes) -> None:
    """Forty accesses per page turn the log into noise that hides the real one."""

    with data_key_scope(user=owner, actor=owner, master_key=master_key):
        for _ in range(40):
            resolve_data_key(user=owner, actor=owner)

    accesses = AuditEvent.objects.filter(user=owner, event_type="encryption_key_accessed")
    assert accesses.count() == 1


def test_a_page_that_decrypts_many_fields_unwraps_once(
    owner: Any, master_key: bytes, monkeypatch: Any
) -> None:
    """The acceptance criterion, through a real request."""

    account = make_account(owner)
    for index in range(10):
        make_transaction(owner, account, amount_encrypted=f"{100 + index}:KRW")
    calls = unwrap_count(monkeypatch)
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("financial-account-list"))

    assert response.status_code == 200
    assert calls[0] == 1
    assert AuditEvent.objects.filter(user=owner, event_type="encryption_key_accessed").count() == 1


def test_the_cache_is_per_request_and_not_shared_between_them(owner: Any, monkeypatch: Any) -> None:
    """Caching across requests would be a key outliving the request that earned it."""

    calls = unwrap_count(monkeypatch)
    client = Client()
    client.force_login(owner)

    client.get(reverse("financial-account-list"))
    client.get(reverse("financial-account-list"))

    assert calls[0] == 2
    assert AuditEvent.objects.filter(user=owner, event_type="encryption_key_accessed").count() == 2


def test_repeated_resolution_inside_one_request_unwraps_once(
    owner: Any, master_key: bytes, monkeypatch: Any
) -> None:
    """What a view, its services, and its template helpers all do in turn."""

    from django.test import RequestFactory

    calls = unwrap_count(monkeypatch)
    request = RequestFactory().get("/accounts/")
    request.user = owner

    keys = [request_data_key(request) for _ in range(25)]

    assert calls[0] == 1
    assert len(set(keys)) == 1


# ----------------------------------------------------------------------
# Nothing left behind
# ----------------------------------------------------------------------


def test_the_key_is_gone_when_the_scope_closes(owner: Any, master_key: bytes) -> None:
    with data_key_scope(user=owner, actor=owner, master_key=master_key):
        assert current_scope() is not None

    assert current_scope() is None


def test_the_key_is_gone_even_when_the_scope_body_raises(owner: Any, master_key: bytes) -> None:
    """A leftover key would be readable by whatever this thread serves next."""

    with (
        pytest.raises(RuntimeError),
        data_key_scope(user=owner, actor=owner, master_key=master_key),
    ):
        raise RuntimeError("the view exploded")

    assert current_scope() is None


def test_a_request_leaves_no_key_behind(owner: Any) -> None:
    client = Client()
    client.force_login(owner)

    client.get(reverse("financial-account-list"))

    assert current_scope() is None


def test_a_failing_request_leaves_no_key_behind(owner: Any, settings: Any) -> None:
    """The middleware clears in a finally, so a 500 is not an exception to it."""

    client = Client(raise_request_exception=False)
    client.force_login(owner)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = "/nonexistent/master.key"

    response = client.get(reverse("financial-account-list"))

    assert response.status_code >= 400
    assert current_scope() is None


def test_a_worker_job_leaves_no_key_behind(owner: Any) -> None:
    from apps.processing.services import process_one_job

    with data_key_scope(user=owner, actor=owner, origin="job"):
        pass
    assert process_one_job() is False
    assert current_scope() is None


# ----------------------------------------------------------------------
# Scoped to one person
# ----------------------------------------------------------------------


def test_a_scope_refuses_to_answer_for_another_user(owner: Any, master_key: bytes) -> None:
    stranger = make_user(email="scope-stranger@example.com", password=PASSWORD)
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)

    with (
        data_key_scope(user=owner, actor=owner, master_key=master_key),
        pytest.raises(KeyScopeError),
    ):
        resolve_data_key(user=stranger, actor=stranger)


def test_nesting_a_scope_for_another_user_is_refused(owner: Any, master_key: bytes) -> None:
    stranger = make_user(email="scope-nested@example.com", password=PASSWORD)
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)

    with (
        data_key_scope(user=owner, actor=owner, master_key=master_key),
        pytest.raises(KeyScopeError),
        data_key_scope(user=stranger, actor=stranger, master_key=master_key),
    ):
        pass


def test_nesting_a_scope_for_the_same_user_reuses_it(
    owner: Any, master_key: bytes, monkeypatch: Any
) -> None:
    """A service that opens one defensively must not double the audit trail."""

    calls = unwrap_count(monkeypatch)

    with data_key_scope(user=owner, actor=owner, master_key=master_key) as outer:  # noqa: SIM117
        with data_key_scope(user=owner, actor=owner, master_key=master_key) as inner:
            assert inner is outer
        # And the inner exit does not close the outer scope.
        assert current_scope() is outer

    assert calls[0] == 1


def test_without_a_scope_the_resolver_still_answers(owner: Any, master_key: bytes) -> None:
    """A management command that never opened one pays the unwrap and works."""

    key = resolve_data_key(user=owner, actor=owner, master_key=master_key)

    assert len(key) == 32
    assert current_scope() is None


# ----------------------------------------------------------------------
# Where the key must never appear
# ----------------------------------------------------------------------


def test_the_key_never_reaches_the_session_or_the_response(owner: Any, master_key: bytes) -> None:
    """A key on the session is a key in the database and in a signed cookie."""

    client = Client()
    client.force_login(owner)
    data_key = resolve_data_key(user=owner, actor=owner, master_key=master_key)
    clear_scope()

    response = client.get(reverse("financial-account-list"))

    assert response.status_code == 200
    encodings = (
        data_key.hex(),
        base64.b64encode(data_key).decode(),
        base64.urlsafe_b64encode(data_key).decode(),
    )
    session = str(dict(client.session))
    body = response.content.decode(errors="ignore")
    for encoded in encodings:
        assert encoded not in session
        assert encoded not in body
    assert data_key not in response.content


def test_the_key_is_not_in_the_template_context(owner: Any) -> None:
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("financial-account-list"))

    contexts = response.context if isinstance(response.context, list) else [response.context]
    for context in contexts:
        flatten = getattr(context, "flatten", None)
        if flatten is None:
            continue
        for value in flatten().values():
            assert not (isinstance(value, bytes) and len(value) == 32), (
                "A 32-byte value reached the template context."
            )
