from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from PIL import Image, UnidentifiedImageError

from apps.core.errors import InvalidRequestError

from .storage import ALLOWED_UPLOAD_TYPES

FORMAT_MIME_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    mime_type: str
    suffix: str
    width: int
    height: int


def validate_uploaded_file(uploaded_file: Any) -> ValidatedUpload:
    """Validate bytes and decoded image format before a file reaches private storage."""

    declared_size = int(getattr(uploaded_file, "size", 0))
    if declared_size <= 0 or declared_size > settings.MAX_UPLOAD_SIZE:
        raise InvalidRequestError("The screenshot is empty or exceeds the upload size limit.")
    try:
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as image:
            detected_mime = FORMAT_MIME_TYPES.get(image.format or "")
            if detected_mime not in ALLOWED_UPLOAD_TYPES:
                raise InvalidRequestError("The screenshot format is not supported.")
            if uploaded_file.content_type and uploaded_file.content_type != detected_mime:
                raise InvalidRequestError("The declared content type does not match the image.")
            image.verify()
            width, height = image.size
    except InvalidRequestError:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise InvalidRequestError("The screenshot could not be decoded.") from exc
    finally:
        uploaded_file.seek(0)
    return ValidatedUpload(
        mime_type=detected_mime,
        suffix=ALLOWED_UPLOAD_TYPES[detected_mime],
        width=width,
        height=height,
    )
