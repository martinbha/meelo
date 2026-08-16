"""Shinhan Bank checking and savings transaction screens."""

from __future__ import annotations

from .base import InstitutionParser, InstitutionProfile

SHINHAN_BANK_PROFILE = InstitutionProfile(
    name="shinhan_bank",
    version="1.0",
    display_name="신한은행",
    institution_markers=("신한은행", "신한 sol", "신한쏠", "shinhan", "sol"),
    layout_markers=("거래일자", "적요", "출금", "입금", "거래후잔액", "잔액"),
    chrome_markers=(
        "조회기간",
        "전체계좌",
        "다음",
        "이전",
        "검색",
        "계좌조회",
        "상세조회",
    ),
    source_type_markers={
        "bank_transfer_confirmation": ("이체결과", "이체완료"),
        "bank_transaction_detail": ("거래상세", "상세내역"),
        "bank_transaction_list": ("거래내역", "거래일자", "적요"),
    },
    default_source_type="bank_transaction_list",
)


class ShinhanBankParser(InstitutionParser):
    """Parses Shinhan checking and savings transaction lists."""

    profile = SHINHAN_BANK_PROFILE
