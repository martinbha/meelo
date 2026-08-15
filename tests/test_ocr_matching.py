from apps.ocr.contracts import BoundingBox, OcrToken
from apps.ocr.matching import (
    MatchStatus,
    TokenCandidate,
    horizontal_placement_distance,
    match_engine_tokens,
    overlap_ratio,
    vertical_center_distance,
)


def candidate(engine: str, text: str, box: BoundingBox) -> TokenCandidate:
    return TokenCandidate.from_token(engine, OcrToken(text, 0.9, box))


def test_spatial_matching_groups_equivalent_tokens_with_coordinate_drift() -> None:
    first_box = BoundingBox(10, 20, 80, 40)
    second_box = BoundingBox(12, 21, 82, 42)
    groups = match_engine_tokens(
        (candidate("primary", "₩42,900", first_box),),
        (candidate("fallback", "42 900 원", second_box),),
    )

    assert groups[0].status == MatchStatus.MATCHED
    assert groups[0].text_similarity == 100
    assert groups[0].region == BoundingBox(10, 20, 82, 42)
    assert overlap_ratio(first_box, second_box) > 0.8
    assert vertical_center_distance(first_box, second_box) < 0.1
    assert horizontal_placement_distance(first_box, second_box) < 0.1


def test_amount_and_date_disagreements_remain_conflicts() -> None:
    groups = match_engine_tokens(
        (
            candidate("primary", "₩42,900", BoundingBox(10, 20, 80, 40)),
            candidate("primary", "2026.8.15", BoundingBox(10, 50, 80, 70)),
        ),
        (
            candidate("fallback", "₩42,800", BoundingBox(12, 21, 82, 42)),
            candidate("fallback", "2026.8.16", BoundingBox(12, 51, 82, 72)),
        ),
    )

    assert [group.status for group in groups] == [MatchStatus.CONFLICT, MatchStatus.CONFLICT]
    assert all(len(group.tokens) == 2 for group in groups)


def test_unmatched_tokens_are_preserved_for_review() -> None:
    groups = match_engine_tokens(
        (candidate("primary", "merchant", BoundingBox(0, 0, 40, 10)),),
        (candidate("fallback", "extra", BoundingBox(0, 100, 30, 110)),),
    )

    assert [group.status for group in groups] == [MatchStatus.UNMATCHED, MatchStatus.UNMATCHED]
    assert [group.tokens[0].text for group in groups] == ["merchant", "extra"]
