import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from apps.core.errors import ConflictError, ForbiddenError
from apps.core.models import AuditEvent
from apps.observations.models import ImportedObservation
from apps.observations.review import accept_observation
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.reconciliation.duplicates import find_duplicate_candidates
from apps.reconciliation.images import (
    PerceptualHashError,
    find_similar,
    hamming_distance,
    is_near_duplicate,
    near_duplicate_detection_enabled,
    perceptual_hash,
)
from apps.reconciliation.matching import MatchProposal
from apps.reconciliation.models import NearDuplicateDocument, ReconciliationMatch
from apps.reconciliation.services import (
    ReconciliationError,
    automatic_merge_enabled,
    confirm_duplicate_match,
    confirm_match,
    facts_from,
    open_matches,
    queue_match_ids,
    record_duplicate_candidates,
    record_match,
    record_near_duplicates,
    record_proposals,
    reject_match,
    unlink_match,
)
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db

KEY = os.urandom(32)
#: Duplicate grouping is keyed; the value is low entropy without one.
SEARCH_KEY = os.urandom(32)


@pytest.fixture
def owner() -> Any:
    return make_user(email="recon-owner@example.com")


def parsed(**overrides: Any) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 8, 15),
        "amount": Decimal("4200"),
        "currency": "KRW",
        "direction": TransactionDirection.DEBIT,
        "merchant": "스타벅스",
        "confidence_factors": {"token_confidence": 0.95, "amount_confidence": 0.95},
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def seed(user: Any, *observations: ParsedObservation, sha: str = "5" * 64) -> Any:
    document = make_document(user, file_sha256=sha)
    run = make_ocr_run(user, document)
    rows = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            observations,
        ),
        data_key=KEY,
        key_version=1,
    ).observations
    return document, rows


# ---------------------------------------------------------------------------
# Near-identical screenshots (#70)
# ---------------------------------------------------------------------------


def make_image(path: Path, *, shade: int, width: int = 64, height: int = 64) -> Path:
    image = Image.new("L", (width, height), shade)
    for y in range(height):
        for x in range(width):
            image.putpixel((x, y), (x * 4 + y * 2 + shade) % 256)
    image.save(path)
    return path


def test_small_image_changes_stay_within_the_threshold(tmp_path: Path) -> None:
    original = make_image(tmp_path / "a.png", shade=0)
    # A recompressed, slightly resized copy of the same screen.
    with Image.open(original) as source:
        source.resize((62, 62)).save(tmp_path / "b.png")

    left = perceptual_hash(original)
    right = perceptual_hash(tmp_path / "b.png")

    assert hamming_distance(left, right) <= 8
    assert is_near_duplicate(left, right) is True


def test_unrelated_screenshots_are_not_near_duplicates(tmp_path: Path) -> None:
    left = perceptual_hash(make_image(tmp_path / "a.png", shade=0))
    noise = Image.new("L", (64, 64))
    for y in range(64):
        for x in range(64):
            noise.putpixel((x, y), (x * 37 + y * 91) % 256)
    noise.save(tmp_path / "c.png")
    right = perceptual_hash(tmp_path / "c.png")

    assert is_near_duplicate(left, right, threshold=2) is False


def test_the_hash_is_stable_and_hexadecimal(tmp_path: Path) -> None:
    path = make_image(tmp_path / "a.png", shade=10)

    first = perceptual_hash(path)

    assert first == perceptual_hash(path)
    assert len(first) == 16
    int(first, 16)


def test_an_unreadable_image_raises_rather_than_returning_a_hash(tmp_path: Path) -> None:
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")

    with pytest.raises(PerceptualHashError):
        perceptual_hash(broken)


def test_mismatched_hashes_are_rejected() -> None:
    with pytest.raises(PerceptualHashError):
        hamming_distance("abcd", "abcdef12")
    with pytest.raises(PerceptualHashError):
        hamming_distance("", "abcd")


def test_the_feature_can_be_disabled_without_affecting_sha256(owner: Any, settings: Any) -> None:
    settings.NEAR_DUPLICATE_DETECTION_ENABLED = False
    first = make_document(owner, file_sha256="a" * 64, perceptual_hash="0000000000000000")
    second = make_document(owner, file_sha256="b" * 64, perceptual_hash="0000000000000000")

    assert near_duplicate_detection_enabled() is False
    assert find_similar(document_id=first.pk, image_hash="0000000000000000", others=[]) == ()
    assert record_near_duplicates(user=owner, pairs=[]) == ()
    # The exact-duplicate signal is untouched by the switch.
    assert first.file_sha256 != second.file_sha256


def test_near_duplicate_links_are_stored_once_per_pair(owner: Any) -> None:
    first = make_document(owner, file_sha256="a" * 64, perceptual_hash="0f0f0f0f0f0f0f0f")
    second = make_document(owner, file_sha256="b" * 64, perceptual_hash="0f0f0f0f0f0f0f0e")

    pairs = find_similar(
        document_id=first.pk,
        image_hash=first.perceptual_hash,
        others=[(second.pk, second.perceptual_hash)],
    )
    record_near_duplicates(user=owner, pairs=pairs)
    record_near_duplicates(user=owner, pairs=pairs)

    assert NearDuplicateDocument.objects.count() == 1
    link = NearDuplicateDocument.objects.get()
    assert link.distance == 1
    assert link.algorithm == "dhash8"


def test_near_duplicates_of_another_users_document_are_refused(owner: Any) -> None:
    mine = make_document(owner, file_sha256="a" * 64, perceptual_hash="0f0f0f0f0f0f0f0f")
    intruder = make_user(email="recon-intruder@example.com")
    theirs = make_document(intruder, file_sha256="b" * 64, perceptual_hash="0f0f0f0f0f0f0f0f")

    pairs = find_similar(
        document_id=mine.pk,
        image_hash=mine.perceptual_hash,
        others=[(theirs.pk, theirs.perceptual_hash)],
    )

    with pytest.raises(ForbiddenError):
        record_near_duplicates(user=owner, pairs=pairs)


# ---------------------------------------------------------------------------
# Candidate persistence (#69, #71)
# ---------------------------------------------------------------------------


def test_duplicate_candidates_are_stored_without_merging_anything(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())
    candidates = find_duplicate_candidates(
        [facts_from(row, merchant="스타벅스", amount_minor=4200) for row in rows],
        search_key=SEARCH_KEY,
    )

    stored = record_duplicate_candidates(user=owner, candidates=candidates, data_key=KEY)

    assert len(stored) == 1
    assert stored[0].status == ReconciliationMatch.Status.PROPOSED
    # No candidate silently deletes or merges an observation.
    for row in rows:
        row.refresh_from_db()
        assert row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
    assert automatic_merge_enabled() is False


def test_re_running_detection_updates_rather_than_duplicating(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())

    record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=70,
    )
    updated = record_match(
        user=owner,
        # The reversed order must resolve to the same stored pair.
        left_observation_id=rows[1].pk,
        right_observation_id=rows[0].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )

    assert ReconciliationMatch.objects.count() == 1
    assert updated.match_score == 95


def test_a_decided_candidate_is_not_revived_by_a_later_detection_run(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=70,
    )
    reject_match(match.pk, user=owner)

    again = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=99,
    )

    assert again.status == ReconciliationMatch.Status.REJECTED
    assert again.match_score == 70


def test_matches_across_users_are_refused(owner: Any) -> None:
    _, mine = seed(owner, parsed(), parsed())
    intruder = make_user(email="match-intruder@example.com")
    _, theirs = seed(intruder, parsed(), sha="6" * 64)

    with pytest.raises(ForbiddenError):
        record_match(
            user=owner,
            left_observation_id=mine[0].pk,
            right_observation_id=theirs[0].pk,
            match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
            score=90,
        )


def test_a_match_needs_two_different_observations(owner: Any) -> None:
    _, rows = seed(owner, parsed())

    with pytest.raises(ReconciliationError):
        record_match(
            user=owner,
            left_observation_id=rows[0].pk,
            right_observation_id=rows[0].pk,
            match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
            score=90,
        )


def test_proposals_of_other_types_are_stored(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())

    stored = record_proposals(
        user=owner,
        proposals=[
            MatchProposal(
                rows[0].pk,
                rows[1].pk,
                ReconciliationMatch.MatchType.INTERNAL_TRANSFER,
                85,
                ("exact_amount",),
            )
        ],
    )

    assert stored[0].match_type == ReconciliationMatch.MatchType.INTERNAL_TRANSFER
    assert stored[0].match_score == 85


def test_queue_match_ids_group_by_review_filter(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed(), parsed())
    record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=90,
    )
    record_match(
        user=owner,
        left_observation_id=rows[1].pk,
        right_observation_id=rows[2].pk,
        match_type=ReconciliationMatch.MatchType.CREDIT_CARD_PAYMENT,
        score=80,
    )

    grouped = queue_match_ids(owner)

    assert set(grouped) == {"duplicate", "settlement"}
    assert rows[0].pk in grouped["duplicate"]
    assert rows[2].pk in grouped["settlement"]


# ---------------------------------------------------------------------------
# Merge workflow (#72)
# ---------------------------------------------------------------------------


def test_confirming_a_duplicate_merges_and_keeps_sources_traceable(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )
    winner_id = match.left_observation_id
    loser_id = match.right_observation_id

    confirmed = confirm_duplicate_match(match.pk, user=owner, winner_id=winner_id)

    loser = ImportedObservation.objects.get(pk=loser_id)
    assert confirmed.status == ReconciliationMatch.Status.CONFIRMED
    assert loser.review_status == ImportedObservation.ReviewStatus.MERGED
    assert loser.merged_into_id == winner_id
    # The merged row keeps its own source document.
    assert loser.source_document_id is not None
    assert AuditEvent.objects.filter(
        user=owner, event_type=AuditEvent.EventType.DUPLICATE_MERGED
    ).exists()


def test_confirming_a_duplicate_twice_is_idempotent(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )

    confirm_duplicate_match(match.pk, user=owner, winner_id=match.left_observation_id)
    confirm_duplicate_match(match.pk, user=owner, winner_id=match.left_observation_id)

    assert (
        ImportedObservation.objects.filter(
            review_status=ImportedObservation.ReviewStatus.MERGED
        ).count()
        == 1
    )


def test_unlinking_a_duplicate_restores_both_rows_and_keeps_match_history(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )
    confirm_duplicate_match(match.pk, user=owner, winner_id=match.left_observation_id)

    unlinked = unlink_match(match.pk, user=owner)

    restored = ImportedObservation.objects.get(pk=match.right_observation_id)
    assert unlinked.status == ReconciliationMatch.Status.REJECTED
    assert restored.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
    assert restored.merged_into_id is None
    assert AuditEvent.objects.filter(
        user=owner, event_type="reconciliation_match_unlinked", object_id=match.pk
    ).exists()


def test_merging_cannot_discard_a_confirmed_transaction(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )
    accept_observation(
        match.right_observation_id,
        user=owner,
        data_key=KEY,
        financial_account=make_account(owner),
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    with pytest.raises(ConflictError):
        confirm_duplicate_match(match.pk, user=owner, winner_id=match.left_observation_id)

    match.refresh_from_db()
    assert match.status == ReconciliationMatch.Status.PROPOSED


def test_the_winner_must_belong_to_the_match(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed(), parsed())
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )

    with pytest.raises(ReconciliationError):
        confirm_duplicate_match(match.pk, user=owner, winner_id=rows[2].pk)


def test_non_duplicate_matches_use_the_plain_confirm_path(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())
    settlement = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.CREDIT_CARD_PAYMENT,
        score=90,
    )

    confirmed = confirm_match(settlement.pk, user=owner)

    assert confirmed.status == ReconciliationMatch.Status.CONFIRMED
    # Confirming a settlement must not merge the observations away.
    for row in rows:
        row.refresh_from_db()
        assert row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED

    with pytest.raises(ReconciliationError):
        confirm_duplicate_match(settlement.pk, user=owner, winner_id=rows[0].pk)


def test_rejecting_a_candidate_leaves_both_observations_alone(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )

    rejected = reject_match(match.pk, user=owner)

    assert rejected.status == ReconciliationMatch.Status.REJECTED
    for row in rows:
        row.refresh_from_db()
        assert row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
    assert open_matches(owner).count() == 0


def test_a_confirmed_match_cannot_be_rejected(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.CREDIT_CARD_PAYMENT,
        score=90,
    )
    confirm_match(match.pk, user=owner)

    with pytest.raises(ConflictError):
        reject_match(match.pk, user=owner)


def test_another_users_match_cannot_be_acted_on(owner: Any) -> None:
    _, rows = seed(owner, parsed(), parsed())
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )
    intruder = make_user(email="confirm-intruder@example.com")

    with pytest.raises(ForbiddenError):
        confirm_duplicate_match(match.pk, user=intruder, winner_id=rows[0].pk)
    with pytest.raises(ForbiddenError):
        reject_match(match.pk, user=intruder)
