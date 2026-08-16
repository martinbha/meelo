"""Kakao Bank transaction and transfer screens."""

from __future__ import annotations

from .base import InstitutionParser, InstitutionProfile

KAKAO_BANK_PROFILE = InstitutionProfile(
    name="kakao_bank",
    version="1.0",
    display_name="카카오뱅크",
    institution_markers=("카카오뱅크", "카뱅", "kakaobank", "kakao bank"),
    layout_markers=("입출금통장", "거래내역", "잔액", "이체"),
    chrome_markers=(
        "전체",
        "내역더보기",
        "이체하기",
        "조회기간",
        "필터",
        "모임통장",
    ),
    source_type_markers={
        "bank_transfer_confirmation": ("이체완료", "송금완료"),
        "bank_transaction_detail": ("거래상세", "상세내역"),
        "bank_transaction_list": ("거래내역", "입출금통장"),
    },
    default_source_type="bank_transaction_list",
)


class KakaoBankParser(InstitutionParser):
    """Parses Kakao Bank list and detail screens."""

    profile = KAKAO_BANK_PROFILE
