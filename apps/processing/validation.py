from __future__ import annotations

import warnings
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


class UploadValidationError(InvalidRequestError):
    """An upload failed a safe boundary check with a stable machine code."""


class ImageDecodeError(UploadValidationError):
    code = "IMAGE_DECODE_FAILED"


class ImageDimensionsTooLargeError(UploadValidationError):
    code = "IMAGE_DIMENSIONS_TOO_LARGE"


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
        Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(uploaded_file) as image:
                detected_mime = FORMAT_MIME_TYPES.get(image.format or "")
                if detected_mime not in ALLOWED_UPLOAD_TYPES:
                    raise InvalidRequestError("The screenshot format is not supported.")
                if uploaded_file.content_type and uploaded_file.content_type != detected_mime:
                    raise InvalidRequestError("The declared content type does not match the image.")
                width, height = image.size
                if width * height > settings.MAX_IMAGE_PIXELS:
                    raise ImageDimensionsTooLargeError(
                        "The screenshot dimensions exceed the safe limit."
                    )
                image.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageDimensionsTooLargeError(
            "The screenshot dimensions exceed the safe limit."
        ) from exc
    except InvalidRequestError:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageDecodeError("The screenshot could not be decoded.") from exc
    finally:
        uploaded_file.seek(0)
    return ValidatedUpload(
        mime_type=detected_mime,
        suffix=ALLOWED_UPLOAD_TYPES[detected_mime],
        width=width,
        height=height,
    )
