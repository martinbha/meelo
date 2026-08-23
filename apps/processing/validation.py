from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import cv2
import numpy as np
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


class FileTooLargeError(UploadValidationError):
    code = "FILE_TOO_LARGE"


class InvalidFileTypeError(UploadValidationError):
    code = "INVALID_FILE_TYPE"


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


def fingerprint_uploaded_file(uploaded_file: Any) -> str:
    digest = hashlib.sha256()
    uploaded_file.seek(0)
    chunks = (
        uploaded_file.chunks()
        if hasattr(uploaded_file, "chunks")
        else iter(lambda: uploaded_file.read(1024 * 1024), b"")
    )
    for chunk in chunks:
        if not chunk:
            break
        digest.update(chunk)
    uploaded_file.seek(0)
    return digest.hexdigest()


def validate_uploaded_file(uploaded_file: Any) -> ValidatedUpload:
    """Validate bytes and decoded image format before a file reaches private storage."""

    declared_size = int(getattr(uploaded_file, "size", 0))
    if declared_size <= 0 or declared_size > settings.MAX_UPLOAD_SIZE:
        raise FileTooLargeError("The screenshot is empty or exceeds the upload size limit.")
    previous_pixel_limit = Image.MAX_IMAGE_PIXELS
    try:
        uploaded_file.seek(0)
        Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(uploaded_file) as image:
                detected_mime = FORMAT_MIME_TYPES.get(image.format or "")
                if detected_mime not in ALLOWED_UPLOAD_TYPES:
                    raise InvalidFileTypeError("The screenshot format is not supported.")
                if uploaded_file.content_type and uploaded_file.content_type != detected_mime:
                    raise InvalidFileTypeError(
                        "The declared content type does not match the image."
                    )
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
        Image.MAX_IMAGE_PIXELS = previous_pixel_limit
        uploaded_file.seek(0)
    return ValidatedUpload(
        mime_type=detected_mime,
        suffix=ALLOWED_UPLOAD_TYPES[detected_mime],
        width=width,
        height=height,
    )


def decode_stored_image(path: Path) -> np.ndarray[Any, Any]:
    """Validate one immutable byte snapshot before allowing OpenCV to allocate it."""

    with path.open("rb") as source:
        payload = source.read(settings.MAX_UPLOAD_SIZE + 1)
    if not payload or len(payload) > settings.MAX_UPLOAD_SIZE:
        raise ImageDecodeError("The stored screenshot is empty or exceeds the size limit.")

    previous_pixel_limit = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
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
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageDecodeError("The stored screenshot could not be decoded.") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_pixel_limit

    decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if decoded is None or decoded.ndim < 2:
        raise ImageDecodeError("OpenCV could not decode the stored screenshot.")
    decoded_height, decoded_width = decoded.shape[:2]
    if decoded_width != width or decoded_height != height:
        raise ImageDecodeError("Image decoders reported inconsistent dimensions.")
    return decoded
