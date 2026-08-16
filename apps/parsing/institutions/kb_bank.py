"""KB Kookmin Bank transaction and transfer screens."""

from __future__ import annotations

from .base import InstitutionParser, InstitutionProfile

KB_BANK_PROFILE = InstitutionProfile(
    name="kb_bank",
    version="1.0",
    display_name="KB국민은행",
    institution_markers=("kb국민은행", "국민은행", "kb스타뱅킹", "kb star", "kookmin", "kb"),
    layout_markers=("거래일", "거래내용", "출금", "입금", "잔액", "거래후잔액"),
    chrome_markers=(
        "조회기간",
        "전체",
        "더보기",
        "계좌선택",
        "이체하기",
        "기간조회",
    ),
    source_type_markers={
        "bank_transfer_confirmation": ("이체확인증", "이체완료"),
        "bank_transaction_detail": ("거래상세", "상세조회"),
        "bank_transaction_list": ("거래내역", "거래일", "거래내용"),
    },
    default_source_type="bank_transaction_list",
)


class KbBankParser(InstitutionParser):
    """Parses KB bank transaction lists and transfer confirmations."""

    profile = KB_BANK_PROFILE
