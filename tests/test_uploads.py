from __future__ import annotations

import os
import shutil
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils.datastructures import MultiValueDict
from PIL import Image

from apps.core.errors import InvalidRequestError
from apps.processing.forms import ScreenshotUploadForm
from apps.processing.models import ProcessingJob, SourceDocument
from apps.processing.storage import document_directory, safe_document_path
from apps.processing.upload_services import DuplicateUploadError, create_uploaded_document
from apps.processing.validation import ImageDimensionsTooLargeError


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("uploader@example.com", password="password")


@pytest.fixture(autouse=True)
def clean_upload_tmp() -> Any:
    yield
    shutil.rmtree("/tmp/finance-ocr-tests", ignore_errors=True)


def screenshot(
    *, name: str = "statement.png", content_type: str = "image/png", size: tuple[int, int] = (2, 2)
) -> Any:
    output = BytesIO()
    image = Image.new("RGB", size, "white")
    image.save(output, format="PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type=content_type)


@pytest.mark.django_db
def test_upload_creates_document_and_queued_job(user: Any) -> None:
    document = create_uploaded_document(user=user, uploaded_file=screenshot())

    assert document.processing_status == SourceDocument.Status.QUEUED
    assert document.file_size > 0
    assert document.temporary_path.endswith("/original.png")
    assert os.stat(document_directory(document.pk)).st_mode & 0o777 == 0o700
    assert os.stat(document.temporary_path).st_mode & 0o777 == 0o600
    assert ProcessingJob.objects.get(document_id=document.pk).task_name == "process_document"
    assert user.audit_events.filter(event_type="screenshot_uploaded").exists()


@pytest.mark.django_db
def test_upload_form_rejects_unsupported_type() -> None:
    form = ScreenshotUploadForm(
        files=MultiValueDict({"screenshot": [screenshot(content_type="text/plain")]})
    )

    assert form.is_valid() is False
    assert "declared content type" in form.errors["screenshot"][0]


@pytest.mark.django_db
def test_upload_routes_are_authenticated_and_owner_scoped(user: Any, client: Any) -> None:
    client.force_login(user)
    assert client.get(reverse("upload-new")).status_code == 200
    document = create_uploaded_document(user=user, uploaded_file=screenshot())
    other = type(user).objects.create_user("other-uploader@example.com", password="password")
    client.force_login(other)

    assert client.get(reverse("upload-detail", args=[document.pk])).status_code == 404


@pytest.mark.django_db
def test_service_rejects_malformed_image_before_persistence(user: Any) -> None:
    with pytest.raises(InvalidRequestError, match="decoded"):
        create_uploaded_document(
            user=user,
            uploaded_file=SimpleUploadedFile(
                "spoof.png", b"not-an-image", content_type="image/png"
            ),
        )

    assert SourceDocument.objects.filter(user=user).count() == 0
    assert ProcessingJob.objects.filter(user=user).count() == 0


@pytest.mark.django_db
@override_settings(MAX_UPLOAD_SIZE=10)
def test_service_rejects_oversized_payload_before_persistence(user: Any) -> None:
    with pytest.raises(InvalidRequestError, match="size limit"):
        create_uploaded_document(user=user, uploaded_file=screenshot())

    assert SourceDocument.objects.filter(user=user).count() == 0


@pytest.mark.django_db
@override_settings(MAX_IMAGE_PIXELS=4)
def test_service_rejects_excessive_image_dimensions_before_persistence(user: Any) -> None:
    with pytest.raises(ImageDimensionsTooLargeError) as raised:
        create_uploaded_document(user=user, uploaded_file=screenshot(size=(3, 2)))

    assert raised.value.code == "IMAGE_DIMENSIONS_TOO_LARGE"
    assert SourceDocument.objects.filter(user=user).count() == 0


@pytest.mark.django_db
def test_duplicate_fingerprint_is_scoped_to_one_user(user: Any) -> None:
    first = create_uploaded_document(user=user, uploaded_file=screenshot())

    with pytest.raises(DuplicateUploadError) as raised:
        create_uploaded_document(user=user, uploaded_file=screenshot())
    assert raised.value.document.pk == first.pk

    other = type(user).objects.create_user("other-fingerprint@example.com", password="password")
    second = create_uploaded_document(user=other, uploaded_file=screenshot())
    assert second.pk != first.pk


@pytest.mark.django_db
def test_storage_rejects_traversal_and_symlink_paths(user: Any) -> None:
    document = create_uploaded_document(user=user, uploaded_file=screenshot())
    with pytest.raises(ValueError, match="outside"):
        safe_document_path(document.pk, Path(document.temporary_path).parent / "../escape")
