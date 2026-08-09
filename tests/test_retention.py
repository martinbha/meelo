from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.core.errors import InvalidRequestError
from apps.core.models import AuditEvent
from apps.processing.models import SourceDocument
from apps.processing.retention import delete_document, expire_documents, retention_deadline


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("retention@example.com", password="password")


def make_document(user: Any, **overrides: Any) -> SourceDocument:
    values: dict[str, Any] = {
        "user": user,
        "file_sha256": uuid4().hex + uuid4().hex,
        "original_filename_encrypted": "retention.png",
        "mime_type": "image/png",
        "file_size": 4,
        "retention_policy": SourceDocument.RetentionPolicy.ONE_DAY,
        "retention_deadline": timezone.now() + timedelta(days=1),
    }
    values.update(overrides)
    return SourceDocument.objects.create(**values)


@pytest.mark.django_db
def test_retention_deadlines_support_all_policies() -> None:
    start = timezone.now()
    assert retention_deadline(SourceDocument.RetentionPolicy.IMMEDIATE, uploaded_at=start) == start
    assert retention_deadline(
        SourceDocument.RetentionPolicy.ONE_DAY, uploaded_at=start
    ) == start + timedelta(days=1)
    assert retention_deadline(
        SourceDocument.RetentionPolicy.SEVEN_DAYS, uploaded_at=start
    ) == start + timedelta(days=7)
    assert retention_deadline(
        SourceDocument.RetentionPolicy.THIRTY_DAYS, uploaded_at=start
    ) == start + timedelta(days=30)


@pytest.mark.django_db
def test_delete_document_is_owner_scoped_idempotent_and_audited(user: Any) -> None:
    document = make_document(user)
    deleted = delete_document(document.pk, user=user)
    again = delete_document(document.pk, user=user)

    assert deleted.processing_status == SourceDocument.Status.DELETED
    assert again.original_deleted_at == deleted.original_deleted_at
    assert user.audit_events.filter(event_type=AuditEvent.EventType.SCREENSHOT_DELETED).count() == 1

    other = type(user).objects.create_user("other-retention@example.com", password="password")
    with pytest.raises(InvalidRequestError):
        delete_document(document.pk, user=other)


@pytest.mark.django_db
def test_expired_documents_are_deleted_without_touching_other_records(
    user: Any, capsys: Any
) -> None:
    expired = make_document(
        user,
        retention_deadline=timezone.now() - timedelta(minutes=1),
        processing_status=SourceDocument.Status.READY_FOR_REVIEW,
    )
    current = make_document(user)
    current.retention_deadline = timezone.now() + timedelta(days=1)
    current.save(update_fields=["retention_deadline"])

    assert expire_documents() == 1
    expired.refresh_from_db()
    current.refresh_from_db()
    assert expired.processing_status == SourceDocument.Status.DELETED
    assert current.processing_status != SourceDocument.Status.DELETED

    call_command("expire_document_retention")
    assert "Deleted 0 expired document(s)." in capsys.readouterr().out


@pytest.mark.django_db
def test_delete_route_is_owner_scoped(user: Any) -> None:
    document = make_document(user)
    client = Client()
    client.force_login(user)
    response = client.post(f"/uploads/{document.pk}/delete/")
    assert response.status_code == 302

    other = type(user).objects.create_user("other-route-retention@example.com", password="password")
    document = make_document(other)
    client.force_login(user)
    assert client.post(f"/uploads/{document.pk}/delete/").status_code == 404
