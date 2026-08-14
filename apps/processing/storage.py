from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from django.conf import settings

from apps.core.errors import InvalidRequestError

ALLOWED_UPLOAD_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


def document_directory(document_id: UUID) -> Path:
    raw_root = Path(settings.DOCUMENT_TMP_ROOT)
    if raw_root.is_symlink():
        raise ValueError("The document temporary root cannot be a symlink.")
    root = raw_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    directory = (root / str(document_id)).resolve()
    if directory.parent != root or directory.name != str(document_id):
        raise ValueError("Invalid document storage path.")
    return directory


def safe_document_path(document_id: UUID, path: str | Path) -> Path:
    directory = document_directory(document_id)
    raw_candidate = Path(path)
    if raw_candidate.is_symlink():
        raise ValueError("The temporary file cannot be a symlink.")
    candidate = raw_candidate.resolve()
    if not candidate.is_relative_to(directory):
        raise ValueError("The temporary file is outside the document directory.")
    return candidate


def store_uploaded_file(
    document_id: UUID, uploaded_file: BinaryIO, *, suffix: str
) -> tuple[Path, str, int]:
    directory = document_directory(document_id)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"original{suffix}"
    digest = hashlib.sha256()
    size = 0
    chunks = (
        uploaded_file.chunks()
        if hasattr(uploaded_file, "chunks")
        else iter(lambda: uploaded_file.read(1024 * 1024), b"")
    )
    try:
        with path.open("wb") as destination:
            for chunk in chunks:
                if not chunk:
                    break
                if size + len(chunk) > settings.MAX_UPLOAD_SIZE:
                    raise InvalidRequestError("The screenshot exceeds the upload size limit.")
                destination.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        directory.rmdir()
        raise
    os.chmod(path, 0o600)
    return path, digest.hexdigest(), size
