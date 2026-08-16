"""Toss Bank transaction and transfer screens."""

from __future__ import annotations

from .base import InstitutionParser, InstitutionProfile

TOSS_BANK_PROFILE = InstitutionProfile(
    name="toss_bank",
    version="1.0",
    display_name="토스뱅크",
    institution_markers=("토스뱅크", "토스", "toss bank", "tossbank", "toss"),
    layout_markers=("입출금", "거래내역", "이체", "잔액"),
    chrome_markers=(
        "전체",
        "더보기",
        "내역 조회",
        "계좌 관리",
        "필터",
        "조회기간",
        "송금하기",
    ),
    source_type_markers={
        "bank_transfer_confirmation": ("이체완료", "송금완료", "이체 결과"),
        "bank_transaction_detail": ("거래상세", "거래 상세"),
        "bank_transaction_list": ("거래내역", "입출금"),
    },
    default_source_type="bank_transaction_list",
)


class TossBankParser(InstitutionParser):
    """Parses Toss Bank list, detail, and transfer-confirmation screens."""

    profile = TOSS_BANK_PROFILE
