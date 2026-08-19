"""The worker decrypting for somebody who is not there (#160, specification 22.2, 26).

A queued screenshot is parsed minutes after the person who uploaded it closed
the tab, and the OCR output has to be sealed under their key or it is not
theirs. So the worker needs the owner's data key with nobody signed in.

The lazy way to allow that is to pass the owner in as their own actor, which
satisfies the ownership check while meaning nothing — the rule becomes "the
worker says this is fine". The worker gets its own door instead, and the rule on
that door is the *document*: the caller does not choose whose key is opened.
"""

from __future__ import annotations

import base64
import os
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image

from apps.core.errors import ForbiddenError
from apps.core.key_management import get_worker_data_key, provision_user_data_key
from apps.core.key_scope import (
    KeyScopeError,
    clear_scope,
    current_scope,
    require_data_key,
    worker_data_key_scope,
)
from apps.processing.models import ProcessingJob, SourceDocument
from apps.processing.services import process_one_job
from apps.processing.storage import document_directory
from tests.factories import make_user

pytestmark = pytest.mark.django_db


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
    user = make_user(email="worker-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture(autouse=True)
def _no_leftover_scope() -> Any:
    clear_scope()
    yield
    clear_scope()


def make_document(user: Any, *, with_file: bool = False) -> SourceDocument:
    document = SourceDocument.objects.create(
        user=user,
        file_sha256=uuid4().hex + uuid4().hex,
        original_filename_encrypted="worker.png",
        mime_type="image/png",
        file_size=4,
        processing_status=SourceDocument.Status.QUEUED,
    )
    if with_file:
        directory = document_directory(document.pk)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "original.png"
        image = BytesIO()
        Image.new("RGB", (2, 2), "white").save(image, format="PNG")
        path.write_bytes(image.getvalue())
        path.chmod(0o600)
        document.temporary_path = str(path)
        document.save(update_fields=["temporary_path"])
    return document


# ----------------------------------------------------------------------
# The worker can work
# ----------------------------------------------------------------------


def test_the_worker_processes_a_document_with_nobody_signed_in(
    owner: Any, monkeypatch: Any
) -> None:
    seen: list[bytes] = []

    def capture(**kwargs: Any) -> tuple[Any, ...]:
        seen.append(require_data_key(user=kwargs["user"]))
        return ()

    monkeypatch.setattr("apps.processing.pipeline.execute_document_ocr", capture)
    document = make_document(owner, with_file=True)
    ProcessingJob.objects.create(user=owner, document_id=document.pk, task_name="process_document")

    assert process_one_job() is True

    document.refresh_from_db()
    assert document.processing_status == SourceDocument.Status.READY_FOR_REVIEW
    assert len(seen) == 1
    assert len(seen[0]) == 32


def test_worker_key_access_names_the_document_in_the_audit_log(
    owner: Any, master_key: bytes
) -> None:
    document = make_document(owner)

    get_worker_data_key(document=document, master_key=master_key)

    event = owner.audit_events.filter(event_type="worker_key_accessed").get()
    assert event.metadata["document_id"] == str(document.pk)
    assert event.object_id == document.pk
    # And it is distinguishable from a person opening their own key.
    assert (
        not owner.audit_events.filter(event_type="encryption_key_accessed")
        .exclude(metadata__document_id__isnull=True)
        .exists()
    )


def test_the_scope_audits_once_for_the_whole_job(owner: Any, master_key: bytes) -> None:
    document = make_document(owner)

    with worker_data_key_scope(document=document, master_key=master_key):
        for _ in range(20):
            require_data_key(user=owner)

    assert owner.audit_events.filter(event_type="worker_key_accessed").count() == 1


# ----------------------------------------------------------------------
# And only for the document's owner
# ----------------------------------------------------------------------


def test_a_job_cannot_reach_another_users_key(owner: Any, master_key: bytes) -> None:
    """There is no user argument to point at the wrong person."""

    stranger = make_user(email="worker-stranger@example.com")
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)
    document = make_document(owner)

    with (
        worker_data_key_scope(document=document, master_key=master_key),
        pytest.raises(KeyScopeError),
    ):
        require_data_key(user=stranger)

    assert not stranger.audit_events.filter(event_type="worker_key_accessed").exists()


def test_a_scope_for_one_owner_refuses_a_document_belonging_to_another(
    owner: Any, master_key: bytes
) -> None:
    stranger = make_user(email="worker-nested@example.com")
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)
    mine = make_document(owner)
    theirs = make_document(stranger)

    with (
        worker_data_key_scope(document=mine, master_key=master_key),
        pytest.raises(KeyScopeError),
        worker_data_key_scope(document=theirs, master_key=master_key),
    ):
        pass


def test_a_deactivated_owners_key_is_not_unwrapped(owner: Any, master_key: bytes) -> None:
    """A suspended account should stop being processed, not quietly continue."""

    document = make_document(owner)
    owner.is_active = False
    owner.save(update_fields=["is_active"])
    document.refresh_from_db()

    with pytest.raises(ForbiddenError):
        get_worker_data_key(document=document, master_key=master_key)

    assert not owner.audit_events.filter(event_type="worker_key_accessed").exists()


# ----------------------------------------------------------------------
# No quiet fallbacks
# ----------------------------------------------------------------------


def test_worker_code_refuses_to_unwrap_outside_a_scope(owner: Any) -> None:
    """A fallback here would restore the rule the worker door exists to replace."""

    with pytest.raises(KeyScopeError, match="must run inside one"):
        require_data_key(user=owner)


def test_the_job_leaves_no_key_behind(owner: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr("apps.processing.pipeline.execute_document_ocr", lambda **kwargs: ())
    document = make_document(owner, with_file=True)
    ProcessingJob.objects.create(user=owner, document_id=document.pk, task_name="process_document")

    assert process_one_job() is True
    assert current_scope() is None


def test_a_failing_job_leaves_no_key_behind(owner: Any, monkeypatch: Any) -> None:
    def explode(**kwargs: Any) -> tuple[Any, ...]:
        raise RuntimeError("ocr fell over")

    monkeypatch.setattr("apps.processing.pipeline.execute_document_ocr", explode)
    document = make_document(owner, with_file=True)
    ProcessingJob.objects.create(user=owner, document_id=document.pk, task_name="process_document")

    assert process_one_job() is True
    assert current_scope() is None
