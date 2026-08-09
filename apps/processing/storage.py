from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from django.conf import settings

ALLOWED_UPLOAD_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


def document_directory(document_id: UUID) -> Path:
    return Path(settings.DOCUMENT_TMP_ROOT) / str(document_id)


def store_uploaded_file(
    document_id: UUID, uploaded_file: BinaryIO, *, suffix: str
) -> tuple[Path, str, int]:
    directory = document_directory(document_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"original{suffix}"
    digest = hashlib.sha256()
    size = 0
    chunks = (
        uploaded_file.chunks()
        if hasattr(uploaded_file, "chunks")
        else iter(lambda: uploaded_file.read(1024 * 1024), b"")
    )
    with path.open("wb") as destination:
        for chunk in chunks:
            if not chunk:
                break
            destination.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return path, digest.hexdigest(), size
