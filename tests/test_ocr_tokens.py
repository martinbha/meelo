import os
from typing import Any

import pytest

from apps.ocr.contracts import BoundingBox, EngineMetadata, OcrConfiguration, OcrRunResult, OcrToken
from apps.ocr.models import OcrToken as StoredOcrToken
from apps.ocr.services import persist_tokens, record_successful_run, serialize_token_for_review
from apps.processing.models import SourceDocument


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("token-owner@example.com", password="password")


def make_document(user: Any) -> SourceDocument:
    return SourceDocument.objects.create(
        user=user,
        file_sha256="1" * 64,
        original_filename_encrypted="encrypted",
        mime_type="image/png",
        file_size=20,
    )


@pytest.mark.django_db
def test_tokens_round_trip_in_reading_order_without_plaintext(user: Any) -> None:
    key = os.urandom(32)
    run = record_successful_run(
        document=make_document(user),
        user=user,
        result=OcrRunResult(
            tokens=(),
            metadata=EngineMetadata("paddleocr", "1"),
            configuration=OcrConfiguration(("ko",)),
            duration_ms=1,
        ),
        data_key=key,
        key_version=1,
    )
    tokens = (
        OcrToken("둘", 0.8, BoundingBox(20, 10, 30, 20), (1, 1, 1, 1, 2)),
        OcrToken("하나", 0.9, BoundingBox(1, 10, 10, 20), (1, 1, 1, 1, 1)),
    )
    stored = persist_tokens(run=run, tokens=tokens, data_key=key, key_version=1)

    assert [token.sequence for token in StoredOcrToken.objects.filter(ocr_run=run)] == [0, 1]
    assert stored[0].text_encrypted != "둘"
    assert stored[0].normalized_text_encrypted != "둘"
    assert (stored[0].left, stored[0].top, stored[0].right, stored[0].bottom) == (20, 10, 30, 20)
    payload = serialize_token_for_review(token=stored[0], user=user, data_key=key)
    assert payload["text"] == "둘"
    assert payload["normalized_text"] == "둘"
    assert payload["bounds"] == {"left": 20, "top": 10, "right": 30, "bottom": 20}
    assert payload["line"] == 1
    assert payload["word"] == 2


@pytest.mark.django_db
def test_token_serializer_rejects_cross_owner_access(user: Any) -> None:
    other = type(user).objects.create_user("token-other@example.com", password="password")
    key = os.urandom(32)
    run = record_successful_run(
        document=make_document(user),
        user=user,
        result=OcrRunResult(
            tokens=(),
            metadata=EngineMetadata("paddleocr", "1"),
            configuration=OcrConfiguration(("en",)),
            duration_ms=0,
        ),
        data_key=key,
        key_version=1,
    )
    token = persist_tokens(
        run=run,
        tokens=(OcrToken("secret", 1, BoundingBox(0, 0, 1, 1)),),
        data_key=key,
        key_version=1,
    )[0]

    with pytest.raises(ValueError, match="requesting user"):
        serialize_token_for_review(token=token, user=other, data_key=key)
