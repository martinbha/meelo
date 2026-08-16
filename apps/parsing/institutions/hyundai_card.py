"""Hyundai Card transaction lists, details, statements, and payments."""

from __future__ import annotations

from .base import InstitutionParser, InstitutionProfile

#: Card screens print no running balance, so the rightmost amount on a row is
#: still the transaction amount rather than a balance column.
HYUNDAI_CARD_PROFILE = InstitutionProfile(
    name="hyundai_card",
    version="1.0",
    display_name="현대카드",
    institution_markers=("현대카드", "hyundai card", "hyundaicard"),
    layout_markers=("이용내역", "승인내역", "승인번호", "할부", "일시불", "청구금액"),
    chrome_markers=(
        "전체",
        "더보기",
        "조회기간",
        "月",
        "카드선택",
        "혜택",
        "포인트",
    ),
    source_type_markers={
        "credit_card_payment": ("결제완료", "납부완료", "대금결제 완료", "출금완료"),
        "credit_card_statement": ("이용대금명세서", "청구서", "명세서"),
        "card_transaction_detail": ("승인상세", "이용상세", "거래상세"),
        "card_transaction_list": ("이용내역", "승인내역"),
    },
    default_source_type="card_transaction_list",
    balance_column=False,
)


class HyundaiCardParser(InstitutionParser):
    """Parses Hyundai Card purchase, statement, and payment screens."""

    profile = HYUNDAI_CARD_PROFILE
