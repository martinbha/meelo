from __future__ import annotations

import shutil
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils.datastructures import MultiValueDict

from apps.processing.forms import ScreenshotUploadForm
from apps.processing.models import ProcessingJob, SourceDocument
from apps.processing.upload_services import create_uploaded_document


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("uploader@example.com", password="password")


@pytest.fixture(autouse=True)
def clean_upload_tmp() -> Any:
    yield
    shutil.rmtree("/tmp/finance-ocr-tests", ignore_errors=True)


def screenshot(*, name: str = "statement.png", content_type: str = "image/png") -> Any:
    return SimpleUploadedFile(name, b"image-bytes", content_type=content_type)


@pytest.mark.django_db
def test_upload_creates_document_and_queued_job(user: Any) -> None:
    document = create_uploaded_document(user=user, uploaded_file=screenshot())

    assert document.processing_status == SourceDocument.Status.QUEUED
    assert document.file_size == len(b"image-bytes")
    assert document.temporary_path.endswith("/original.png")
    assert ProcessingJob.objects.get(document_id=document.pk).task_name == "process_document"
    assert user.audit_events.filter(event_type="screenshot_uploaded").exists()


@pytest.mark.django_db
def test_upload_form_rejects_unsupported_type() -> None:
    form = ScreenshotUploadForm(
        files=MultiValueDict({"screenshot": [screenshot(content_type="text/plain")]})
    )

    assert form.is_valid() is False
    assert "PNG" in form.errors["screenshot"][0]


@pytest.mark.django_db
def test_upload_routes_are_authenticated_and_owner_scoped(user: Any, client: Any) -> None:
    client.force_login(user)
    assert client.get(reverse("upload-new")).status_code == 200
    document = create_uploaded_document(user=user, uploaded_file=screenshot())
    other = type(user).objects.create_user("other-uploader@example.com", password="password")
    client.force_login(other)

    assert client.get(reverse("upload-detail", args=[document.pk])).status_code == 404
